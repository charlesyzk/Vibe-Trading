"""Fail-closed, persistence-before-send IB API command boundary.

This class never connects to a Gateway. A trusted service injects an already
constructed EClient and a durable ledger port. The service must resolve the
contract/order objects and perform the authoritative second risk check before
calling :meth:`submit`.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class DurableSubmissionLedger(Protocol):
    def start_send(self, intent_id: str, *, attempt_id: str, client_order_id: str) -> Any:
        """Commit SEND_STARTED and return only after the transaction commits."""

    def mark_unknown(self, intent_id: str) -> Any:
        """Freeze an uncertain send so it cannot be retried automatically."""


class DispatchDenied(RuntimeError):
    pass


class IbapiOrderSubmitter:
    """Guard an official ``EClient.placeOrder`` call with risk and durable state."""

    def __init__(
        self,
        *,
        client: Any,
        ledger: DurableSubmissionLedger,
        pre_dispatch_check: Callable[[str], bool],
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._pre_dispatch_check = pre_dispatch_check

    def submit(
        self,
        *,
        intent_id: str,
        attempt_id: str,
        client_order_id: str,
        broker_order_id: int,
        contract: Any,
        order: Any,
    ) -> None:
        """Submit one exact SDK order after risk and SEND_STARTED commit.

        Any exception from the SDK call moves the intent to UNKNOWN and is
        re-raised. The caller must reconcile broker state; this object never
        retries a send because the remote outcome may be uncertain.
        """
        if not self._pre_dispatch_check(intent_id):
            raise DispatchDenied("pre-dispatch risk or identity check rejected the order")
        self._ledger.start_send(intent_id, attempt_id=attempt_id, client_order_id=client_order_id)
        self.submit_precommitted(
            intent_id=intent_id,
            broker_order_id=broker_order_id,
            contract=contract,
            order=order,
        )

    def submit_precommitted(
        self,
        *,
        intent_id: str,
        broker_order_id: int,
        contract: Any,
        order: Any,
    ) -> None:
        """Place an order whose send state/mapping committed in a host ledger.

        This is the composition point for the Backtrader execution service,
        which commits ``SEND_STARTED`` and broker mapping atomically before
        entering this SDK-only runtime.
        """
        if type(broker_order_id) is not int or broker_order_id <= 0:
            raise DispatchDenied("broker order id must be a positive integer")
        if not self._pre_dispatch_check(intent_id):
            raise DispatchDenied("pre-dispatch risk or identity check rejected the order")
        try:
            # EClient.placeOrder is the first external side effect. The durable
            # durable send boundary must already be committed before this line.
            self._client.placeOrder(broker_order_id, contract, order)
        except Exception:
            self._ledger.mark_unknown(intent_id)
            raise
