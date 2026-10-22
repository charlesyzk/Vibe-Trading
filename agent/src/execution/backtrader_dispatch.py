"""Trusted offline composition from native Backtrader orders to the paper ledger.

This adapter has no IBKR SDK dependency. The host supplies an intent resolver,
an order-id allocator, a durable callback pump, and a fake or real transport.
The callback pump writes the broker-id binding before the transport is called,
so even a synchronous broker callback can resolve its authorized intent.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
from threading import RLock
from typing import Protocol

from src.bt_runtime.brokers.proxy import (
    BrokerOrderCommand,
    BrokerPort,
    BrokerCommandUncertain,
    BrokerSubmissionUncertain,
    PortOrderRejected,
)
from src.execution.contracts import Environment
from src.execution.dispatch_service import DispatchResult, PaperOrderDispatchService
from src.execution.ibkr_callback_pump import BrokerOrderBinding, ContractIdentity, IbkrCallbackPump
from src.execution.order_ledger import LocalOrderState, SQLiteOrderLedger
from src.execution.owner_lease import ExecutionOwnerLease


class NativeIntentResolver(Protocol):
    def __call__(self, command: BrokerOrderCommand) -> str:
        """Resolve a native order using host-owned run/event/action context."""


class PaperTransport(Protocol):
    def __call__(
        self,
        command: BrokerOrderCommand,
        *,
        intent_id: str,
        broker_order_id: int,
        attempt_id: str,
        client_order_id: str,
    ) -> None:
        """Send one already authorized order; exceptions mean outcome unknown."""


class OneShotLedgerIntentResolver:
    """Bind one host-selected durable intent to the next native BT order.

    The caller arms this resolver only after a committed outbox proposal has
    been authorized and linked to an order-ledger intent. It is consumed by
    the synchronous ``BrokerBase.buy/sell`` call, then disarmed even if order
    construction fails. Uploaded strategy payloads never select an intent ID.
    """

    def __init__(self, ledger: SQLiteOrderLedger) -> None:
        self.ledger = ledger
        self._lock = RLock()
        self._pending_intent_id: str | None = None

    @contextmanager
    def bind_next(self, intent_id: str):
        if not isinstance(intent_id, str) or not intent_id:
            raise ValueError("intent_id is required")
        with self._lock:
            if self._pending_intent_id is not None:
                raise PortOrderRejected("another durable intent is already armed")
            self._pending_intent_id = intent_id
        try:
            yield
        finally:
            with self._lock:
                if self._pending_intent_id == intent_id:
                    self._pending_intent_id = None

    def __call__(self, command: BrokerOrderCommand) -> str:
        with self._lock:
            intent_id = self._pending_intent_id
            self._pending_intent_id = None
        if intent_id is None:
            raise PortOrderRejected("native order has no host-armed durable intent")
        try:
            intent = self.ledger.get_intent(intent_id)
            record = self.ledger.get_order(intent_id)
        except Exception as exc:
            raise PortOrderRejected("host-armed durable intent is unavailable") from exc
        if record.state not in {LocalOrderState.CREATED, LocalOrderState.RESERVED}:
            raise PortOrderRejected("host-armed durable intent is not sendable")
        mismatches = []
        if intent.instrument_id != command.instrument:
            mismatches.append("instrument")
        if intent.side != command.side:
            mismatches.append("side")
        if intent.integer_qty != command.quantity:
            mismatches.append("quantity")
        if intent.order_type != command.order_type:
            mismatches.append("order_type")
        if intent.time_in_force != command.time_in_force:
            mismatches.append("time_in_force")
        if mismatches:
            raise PortOrderRejected("native order differs from armed intent: " + ",".join(mismatches))
        return intent_id


class BacktraderDispatchPort(BrokerPort):
    """Compose native order validation, risk dispatch, durable mapping, and callbacks."""

    def __init__(
        self,
        *,
        ledger: SQLiteOrderLedger,
        dispatch: PaperOrderDispatchService,
        callback_pump: IbkrCallbackPump,
        resolve_intent: NativeIntentResolver,
        resolve_contract_identity: Callable[[str], ContractIdentity],
        owner_lease: ExecutionOwnerLease,
        allocate_order_id: Callable[[], int],
        observe_next_valid_id: Callable[[int], None],
        transport: PaperTransport,
        cancel_transport: Callable[[int], None] | None = None,
        now: Callable[[], datetime] | None = None,
        callback_worker_owns_pump: bool = False,
    ) -> None:
        self.ledger = ledger
        self.dispatch = dispatch
        self.callback_pump = callback_pump
        self.resolve_intent = resolve_intent
        if not callable(resolve_contract_identity):
            raise TypeError("a verified contract identity resolver is required")
        self.resolve_contract_identity = resolve_contract_identity
        self.owner_lease = owner_lease
        self.allocate_order_id = allocate_order_id
        if not callable(observe_next_valid_id):
            raise TypeError("a durable nextValidId observer is required")
        self.callback_pump.next_valid_id_sink = observe_next_valid_id
        self.transport = transport
        self.cancel_transport = cancel_transport
        self.now = now or (lambda: datetime.now(timezone.utc))
        if type(callback_worker_owns_pump) is not bool:
            raise TypeError("callback_worker_owns_pump must be a boolean")
        self.callback_worker_owns_pump = callback_worker_owns_pump
        self.callback_pump.defer_delivery_ack = True
        self._callback_sink: Callable | None = None
        self._native_bindings: dict[int, str] = {}
        self._used_intents: set[str] = set()

    @contextmanager
    def bind_recovered_intent(self, intent_id: str):
        """Bind one host-verified, still-CREATED intent to a new native order.

        Recovery callers must first prove the exact committed outbox link and
        keep the execution service PAUSED. This final check prevents an intent
        that advanced concurrently from being rebound as a fresh order.
        """
        try:
            record = self.ledger.get_order(intent_id)
        except Exception as exc:
            raise PortOrderRejected("recovered ledger intent is unavailable") from exc
        if record.state is not LocalOrderState.CREATED:
            raise PortOrderRejected("only an unchanged CREATED intent can be rebound")
        with self.resolve_intent.bind_next(intent_id):
            yield

    def register_callback_sink(self, sink) -> None:
        self._callback_sink = sink
        self.callback_pump.callback_sink = sink

    def process_callbacks(self) -> None:
        """Consume callbacks only when this port owns the pump consumer role."""
        if self.callback_worker_owns_pump or self.callback_pump.background_worker_registered:
            return
        while True:
            result = self.callback_pump.process_one()
            if result.disposition == "EMPTY":
                return
            if result.callback is not None:
                # PortBroker consumes and acknowledges this queued cumulative
                # state immediately after this method returns. Yield here so
                # an unacknowledged durable inbox item is not replayed in-loop.
                return

    def acknowledge_callback(self, event_id: str) -> None:
        self.callback_pump.acknowledge_delivery(event_id)

    def submit_order(self, command: BrokerOrderCommand) -> None:
        if self._callback_sink is None:
            raise PortOrderRejected("broker callback sink is not registered")
        try:
            intent_id = self.resolve_intent(command)
            if not isinstance(intent_id, str) or not intent_id:
                raise ValueError("trusted resolver returned no intent")
            if command.order_ref in self._native_bindings:
                if self._native_bindings[command.order_ref] == intent_id:
                    raise PortOrderRejected("native order ref was already submitted")
                raise PortOrderRejected("native order ref is already bound to another intent")
            if intent_id in self._used_intents:
                raise PortOrderRejected("authorized intent was already consumed by a native order")
            intent = self.ledger.get_intent(intent_id)
            self.owner_lease.assert_scope(
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
            )
            self._validate_command(command, intent)
            contract = self.resolve_contract_identity(intent.instrument_id)
            if (not isinstance(contract, ContractIdentity) or contract.contract_id <= 0
                    or contract.symbol != intent.instrument_id or contract.security_type != "STK"
                    or contract.currency != "USD"):
                raise ValueError("resolved IBKR contract identity is not a verified USD stock for the intent")
            broker_order_id = self.allocate_order_id()
            if type(broker_order_id) is not int or broker_order_id <= 0:
                raise ValueError("broker order id allocator returned an invalid id")
        except PortOrderRejected:
            raise
        except Exception as exc:
            raise PortOrderRejected(f"native order could not be bound to an authorized intent: {exc}") from exc

        self._native_bindings[command.order_ref] = intent_id
        self._used_intents.add(intent_id)
        broker_binding = {
            "broker_order_id": broker_order_id,
            "account_ref": intent.account_ref,
            "environment": intent.environment.value,
            "intent_id": intent_id,
            "bt_order_ref": command.order_ref,
            "requested_qty": intent.integer_qty,
            "side": intent.side,
            "contract_id": contract.contract_id,
            "symbol": contract.symbol,
            "security_type": contract.security_type,
            "currency": contract.currency,
        }

        def send_after_durable_start(intent, *, attempt_id: str, client_order_id: str) -> None:
            durable_intent = self.ledger.get_intent(intent_id)
            if intent != durable_intent:
                raise BrokerSubmissionUncertain("dispatch changed the bound intent")
            binding = BrokerOrderBinding(
                broker_order_id=broker_order_id,
                account_ref=durable_intent.account_ref,
                environment=durable_intent.environment,
                intent_id=intent_id,
                bt_order_ref=command.order_ref,
                requested_qty=durable_intent.integer_qty,
                side=durable_intent.side,
                contract_id=contract.contract_id,
                symbol=contract.symbol,
                security_type=contract.security_type,
                currency=contract.currency,
            )
            # bind_order verifies committed SEND_STARTED and persists before
            # transport invocation; synchronous callbacks are then resolvable.
            self.callback_pump.bind_order(binding)
            self.transport(
                command,
                intent_id=intent_id,
                broker_order_id=broker_order_id,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
            )

        try:
            result: DispatchResult = self.dispatch.dispatch(
                intent_id, sender=send_after_durable_start, broker_binding=broker_binding
            )
        except PortOrderRejected:
            raise
        except Exception as exc:
            current = self.ledger.get_order(intent_id)
            if current.state in {LocalOrderState.CREATED, LocalOrderState.RESERVED}:
                raise PortOrderRejected(
                    f"authorized order failed before the durable send boundary: {exc}"
                ) from exc
            raise BrokerSubmissionUncertain(
                f"authorized order {command.order_ref} has unresolved dispatch outcome"
            ) from exc
        if result.status == "REJECTED":
            raise PortOrderRejected(
                "risk/dispatch rejected authorized intent: " + ",".join(result.reason_codes)
            )
        if result.status != "SEND_STARTED":
            raise BrokerSubmissionUncertain(
                f"authorized intent dispatch is {result.status}; reconcile before retry"
            )

    def cancel_order(self, command: BrokerOrderCommand) -> None:
        if self.cancel_transport is None:
            raise PortOrderRejected("trusted cancel transport is unavailable")
        try:
            binding = self.callback_pump.order_index.resolve_backtrader_ref(command.order_ref)
            intent = self.ledger.get_intent(binding.intent_id)
            self.owner_lease.assert_scope(
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
            )
            self._validate_command(command, intent)
            if binding.bt_order_ref != command.order_ref or binding.side != command.side:
                raise ValueError("native order differs from the durable broker mapping")
            if binding.requested_qty != command.quantity or binding.symbol != command.instrument:
                raise ValueError("native order differs from the durable broker mapping")
            cancel_id = "cancel:" + hashlib.sha256(
                binding.intent_id.encode("utf-8")
            ).hexdigest()[:32]
            broker_order_id = self.ledger.start_cancel(
                binding.intent_id, cancel_id=cancel_id, now=self.now()
            )
        except PortOrderRejected:
            raise
        except Exception as exc:
            raise PortOrderRejected(f"cancel is not authorized for this native order: {exc}") from exc
        try:
            self.cancel_transport(broker_order_id)
        except Exception as exc:
            try:
                self.ledger.mark_cancel_unknown(binding.intent_id)
            except Exception as mark_exc:
                raise BrokerCommandUncertain(
                    f"cancel outcome for order {command.order_ref} is unknown and could not be frozen"
                ) from mark_exc
            raise BrokerCommandUncertain(
                f"cancel outcome for order {command.order_ref} is unknown; reconcile before retry"
            ) from exc

    @staticmethod
    def _validate_command(command: BrokerOrderCommand, intent) -> None:
        mismatches = []
        if intent.environment is not Environment.PAPER:
            mismatches.append("environment")
        if intent.broker != "IBKR":
            mismatches.append("broker")
        if intent.instrument_id != command.instrument:
            mismatches.append("instrument")
        if intent.side != command.side:
            mismatches.append("side")
        if intent.integer_qty != command.quantity:
            mismatches.append("quantity")
        if intent.order_type != command.order_type:
            mismatches.append("order_type")
        if intent.time_in_force != command.time_in_force:
            mismatches.append("time_in_force")
        if mismatches:
            raise ValueError("native order differs from authorized intent: " + ",".join(mismatches))
