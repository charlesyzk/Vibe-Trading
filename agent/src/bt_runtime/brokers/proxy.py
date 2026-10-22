"""A small Backtrader ``BrokerBase`` proxy with a bounded callback boundary.

The injected port is trusted to bind broker/account/environment identity and
perform authorization and risk checks before accepting either command. It is
also responsible for delivering callbacks through the registered sink. SDK
callbacks only enqueue immutable values; ``next`` on Cerebro's thread is the
only place this broker mutates Backtrader orders, positions, cash, or notices.

This is an integration slice, not a durable execution service: it has no
ledger or account reconciliation. It can rebuild native refs and stage one
host-verified CREATED intent in memory, but durable lineage and recovery gates
belong to the managed execution layer. It has no broker SDK dependency.
"""

from __future__ import annotations

import math
import queue
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol

import backtrader as bt
from backtrader.position import Position


_MAX_ORDER_REF = (1 << 63) - 1


class CallbackStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class BrokerOrderCommand:
    """An exact Backtrader order request passed to the trusted port."""

    order_ref: int
    instrument: str
    side: str
    quantity: int
    order_type: str = "market"
    time_in_force: str = "DAY"


@dataclass(frozen=True, slots=True)
class BrokerCallback:
    """Normalized callback; fill quantities and commission are cumulative."""

    order_ref: int
    status: CallbackStatus | str
    event_id: str
    occurred_at: datetime
    filled_qty: int = 0
    average_fill_price: float | None = None
    cumulative_commission: float = 0.0

    def __post_init__(self) -> None:
        if type(self.order_ref) is not int or self.order_ref <= 0:
            raise ValueError("order_ref must be a positive integer")
        object.__setattr__(self, "status", CallbackStatus(self.status))
        if not isinstance(self.event_id, str) or not self.event_id or len(self.event_id) > 256:
            raise ValueError("event_id must contain 1 to 256 characters")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))
        if type(self.filled_qty) is not int or self.filled_qty < 0:
            raise ValueError("filled_qty must be a non-negative integer")
        if not math.isfinite(self.cumulative_commission) or self.cumulative_commission < 0:
            raise ValueError("cumulative_commission must be finite and non-negative")
        if self.average_fill_price is not None and (
            not math.isfinite(self.average_fill_price) or self.average_fill_price <= 0
        ):
            raise ValueError("average_fill_price must be finite and positive")
        if self.status in {CallbackStatus.PARTIALLY_FILLED, CallbackStatus.FILLED}:
            if self.filled_qty <= 0 or self.average_fill_price is None:
                raise ValueError("fill callbacks require positive cumulative quantity and price")


@dataclass(frozen=True, slots=True)
class ReconciledFill:
    """One immutable broker execution restored into Backtrader order bits."""

    execution_id: str
    quantity: int
    price: float
    occurred_at: datetime
    commission: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id is required for reconciled fills")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("reconciled fill quantity must be a positive integer")
        for name in ("price", "commission"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"reconciled fill {name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or (number <= 0 if name == "price" else number < 0):
                raise ValueError(f"reconciled fill {name} is outside its valid range")
            object.__setattr__(self, name, number)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("reconciled fill time must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))


@dataclass(frozen=True, slots=True)
class ReconciledOrderState:
    """Host-verified facts for rebuilding a previously broker-bound BUY.

    This is intentionally a small recovery primitive, not an authorization
    input. The caller must already have reconciled the ledger binding, broker
    order and account snapshot. Restoring an order never invokes the port.
    BUY recovery carries every execution row so native Backtrader bits are
    rebuilt without inventing a single aggregate fill. SELL remains unsupported
    until its per-fill position-closing and cost-basis semantics are verified.
    """

    order_ref: int
    instrument: str
    side: str
    requested_qty: int
    reference_price: float
    status: CallbackStatus | str
    filled_qty: int
    average_fill_price: float | None
    cumulative_commission: float
    occurred_at: datetime | None
    position_size_after: int
    position_average_price_after: float
    fills: tuple[ReconciledFill, ...] = ()

    def __post_init__(self) -> None:
        for name in ("order_ref", "requested_qty"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.order_ref >= _MAX_ORDER_REF:
            raise ValueError("order_ref leaves no safe broker-local reference for subsequent orders")
        if type(self.filled_qty) is not int or not 0 <= self.filled_qty <= self.requested_qty:
            raise ValueError("filled_qty must be an integer within the requested quantity")
        if type(self.position_size_after) is not int or self.position_size_after < 0:
            raise ValueError("position_size_after must be a non-negative whole-share count")
        if not isinstance(self.instrument, str) or not self.instrument.strip():
            raise ValueError("instrument is required")
        if self.side != "BUY":
            raise ValueError("only BUY order recovery is supported by this aggregate fill record")

        def finite_positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite number")
            number = float(value)
            if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be finite and {qualifier}")
            return number

        object.__setattr__(self, "reference_price", finite_positive(self.reference_price, "reference_price"))
        object.__setattr__(
            self, "cumulative_commission",
            finite_positive(self.cumulative_commission, "cumulative_commission", allow_zero=True),
        )
        object.__setattr__(
            self, "position_average_price_after",
            finite_positive(self.position_average_price_after, "position_average_price_after", allow_zero=True),
        )
        status = CallbackStatus(self.status)
        object.__setattr__(self, "status", status)
        if status not in {
            CallbackStatus.SUBMITTED, CallbackStatus.ACCEPTED,
            CallbackStatus.PARTIALLY_FILLED, CallbackStatus.FILLED,
        }:
            raise ValueError("recovery supports only submitted, accepted, partially filled, or filled orders")
        if status is CallbackStatus.PARTIALLY_FILLED:
            if not 0 < self.filled_qty < self.requested_qty:
                raise ValueError("partial order recovery requires a nonzero incomplete fill")
        elif status is CallbackStatus.FILLED:
            if self.filled_qty != self.requested_qty:
                raise ValueError("filled order recovery requires the full requested quantity")
        elif self.filled_qty != 0:
            raise ValueError("unfilled order recovery status cannot carry execution quantity")
        elif self.cumulative_commission != 0:
            raise ValueError("unfilled order recovery cannot carry commission")

        if self.filled_qty:
            if self.average_fill_price is None:
                raise ValueError("filled order recovery requires an average fill price")
            object.__setattr__(
                self, "average_fill_price", finite_positive(self.average_fill_price, "average_fill_price"),
            )
            if self.occurred_at is None or self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
                raise ValueError("filled order recovery requires an aware execution timestamp")
            object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))
            if self.position_size_after < self.filled_qty or self.position_average_price_after <= 0:
                raise ValueError("reconciled BUY position must include the restored fill")
            fills = tuple(self.fills)
            if not fills or any(not isinstance(fill, ReconciledFill) for fill in fills):
                raise ValueError("filled order recovery requires validated per-execution facts")
            if len({fill.execution_id for fill in fills}) != len(fills):
                raise ValueError("reconciled execution IDs must be unique")
            if tuple(sorted(fills, key=lambda fill: (fill.occurred_at, fill.execution_id))) != fills:
                raise ValueError("reconciled fills must be in strict chronological order")
            fill_qty = sum(fill.quantity for fill in fills)
            fill_notional = sum(fill.quantity * fill.price for fill in fills)
            fill_commission = sum(fill.commission for fill in fills)
            weighted_price = fill_notional / fill_qty
            if fill_qty != self.filled_qty:
                raise ValueError("reconciled executions do not match the cumulative fill quantity")
            if not math.isclose(weighted_price, self.average_fill_price, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError("reconciled executions do not match the cumulative average fill price")
            if fills[-1].occurred_at != self.occurred_at:
                raise ValueError("reconciled executions do not match the latest fill time")
            if not math.isclose(fill_commission, self.cumulative_commission, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError("reconciled executions do not match cumulative commission")
            object.__setattr__(self, "fills", fills)
        elif self.average_fill_price is not None or self.occurred_at is not None:
            raise ValueError("unfilled order recovery cannot carry execution facts")
        elif self.fills:
            raise ValueError("unfilled order recovery cannot carry execution rows")
        elif self.position_size_after and self.position_average_price_after <= 0:
            raise ValueError("nonzero reconciled position requires a positive average price")
        elif not self.position_size_after and self.position_average_price_after != 0:
            raise ValueError("flat reconciled position must have zero average price")


class CallbackQueueFull(RuntimeError):
    """Raised in the callback thread when the bounded handoff is saturated."""


class PortOrderRejected(RuntimeError):
    """The trusted port proves it rejected an order before dispatch began."""


class BrokerSubmissionUncertain(RuntimeError):
    """Submission may have reached the broker; stop and reconcile before retry."""


class BrokerCommandUncertain(RuntimeError):
    """A cancel command may have reached the broker; stop and reconcile."""


class BrokerPort(Protocol):
    """Trusted port contract. Implementations own identity and risk decisions."""

    def register_callback_sink(self, sink) -> None:
        """Register a thread-safe sink accepting one ``BrokerCallback``."""

    def submit_order(self, command: BrokerOrderCommand) -> None:
        """Authorize/submit; raise PortOrderRejected only before dispatch."""

    def cancel_order(self, command: BrokerOrderCommand) -> None:
        """Authorize and request cancellation for this exact order."""


class PortBroker(bt.BrokerBase):
    """Route native Backtrader orders through a trusted, injected port.

    ``initial_cash`` and position state form an in-process execution view for
    this slice. A production port must provide authoritative account snapshots
    and ledger-backed updates before using these methods for deployed trading.
    """

    params = (("callback_queue_size", 256), ("initial_cash", 100_000.0))

    def __init__(
        self,
        port: BrokerPort,
        *,
        recovered_dispatch_ready: Callable[[], bool] | None = None,
        on_broker_cycle: Callable[[], None] | None = None,
    ):
        super().__init__()
        if port is None:
            raise ValueError("a trusted broker port is required")
        if self.p.callback_queue_size < 1:
            raise ValueError("callback_queue_size must be positive")
        if not math.isfinite(float(self.p.initial_cash)) or self.p.initial_cash < 0:
            raise ValueError("initial_cash must be finite and non-negative")
        self.port = port
        self._callback_queue: queue.Queue[BrokerCallback] = queue.Queue(
            maxsize=int(self.p.callback_queue_size)
        )
        self._notifications: deque[bt.Order] = deque()
        self.orders: dict[int, bt.Order] = {}
        # Backtrader's default Order.refbasis is process-global and resets on
        # process restart. Allocate refs per broker so restored durable refs
        # cannot collide with later orders in this broker instance.
        self._next_order_ref = 1
        self._seen_event_ids: dict[str, BrokerCallback] = {}
        self._positions: defaultdict[bt.LineSeries, Position] = defaultdict(Position)
        self._cash = float(self.p.initial_cash)
        self.startingcash = self._cash
        self._callback_error: BaseException | None = None
        self._lifecycle_lock = threading.RLock()
        self._start_called = False
        self._snapshot_hydrated = False
        if recovered_dispatch_ready is not None and not callable(recovered_dispatch_ready):
            raise TypeError("recovered_dispatch_ready must be callable")
        if on_broker_cycle is not None and not callable(on_broker_cycle):
            raise TypeError("on_broker_cycle must be callable")
        self._recovered_dispatch_ready = recovered_dispatch_ready
        self._on_broker_cycle = on_broker_cycle
        self._recovered_intents: deque[tuple[Any, str, str, int, float, str]] = deque()
        self._recovered_intent_ids: set[str] = set()

    def queue_recovered_intent(
        self, *, data: Any, instrument: str, side: str, quantity: int,
        reference_price: float, intent_id: str,
    ) -> None:
        """Stage one exact CREATED intent for explicit post-recovery activation.

        Staging never dispatches. The queue is drained only by ``next`` on the
        Cerebro thread, and only while the managed session's send gate is open.
        """
        if self._start_called or not self._snapshot_hydrated:
            raise RuntimeError("recovered intents must be queued after hydration and before broker start")
        if data is None or getattr(data, "_name", None) != instrument:
            raise ValueError("recovered intent instrument is outside the frozen feed set")
        if side not in {"BUY", "SELL"} or type(quantity) is not int or quantity <= 0:
            raise ValueError("recovered intent requires a supported side and positive whole-share quantity")
        if not math.isfinite(float(reference_price)) or reference_price <= 0:
            raise ValueError("recovered intent requires a finite positive reference price")
        if not isinstance(intent_id, str) or not intent_id or intent_id in self._recovered_intent_ids:
            raise ValueError("recovered intent id is invalid or duplicated")
        self._recovered_intents.append((data, instrument, side, quantity, float(reference_price), intent_id))
        self._recovered_intent_ids.add(intent_id)

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_called = True
            super().start()
            register = getattr(self.port, "register_callback_sink", None)
            if register is not None:
                register(self.on_callback)

    def hydrate_reconciled_account_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        account_ref: str,
        environment: str,
        data_by_con_id: Mapping[int, Any],
    ) -> None:
        """Restore initial cash and positions from a verified PAPER snapshot.

        This is a one-shot pre-start operation. The snapshot must be the
        completed output of the trusted reconciliation store, including its
        durable reconciliation proof. A reported contract id must map to one
        unique Backtrader data object; no ticker-name inference is performed.
        """
        with self._lifecycle_lock:
            self._assert_hydration_idle()
            if not isinstance(snapshot, Mapping):
                raise ValueError("account snapshot must be a mapping")
            if not isinstance(account_ref, str) or not account_ref.strip():
                raise ValueError("account_ref is required")
            if environment != "paper":
                raise ValueError("PortBroker snapshot hydration accepts PAPER only")
            if (
                snapshot.get("account_ref") != account_ref
                or snapshot.get("environment") != "paper"
                or snapshot.get("capture_complete") is not True
                or snapshot.get("reconciled") is not True
            ):
                raise ValueError("snapshot must be complete, reconciled, and match PAPER account scope")

            snapshot_id = snapshot.get("snapshot_id")
            if type(snapshot_id) is not int or snapshot_id <= 0:
                raise ValueError("snapshot_id must be a positive integer")
            if not snapshot.get("requested_at_utc") or not snapshot.get("completed_at_utc"):
                raise ValueError("snapshot must include its requested and completed timestamps")
            proof = snapshot.get("reconciliation_result")
            if (
                not isinstance(proof, Mapping)
                or proof.get("snapshot_id") != snapshot_id
                or proof.get("account_ref") != account_ref
                or proof.get("environment") != "paper"
                or not isinstance(proof.get("checks"), list)
                or not {
                    "scope", "open_orders", "positions", "order_id_high_water_mark"
                }.issubset(proof["checks"])
            ):
                raise ValueError("snapshot has no matching durable reconciliation proof")

            summary = snapshot.get("account_summary")
            positions = snapshot.get("positions")
            open_orders = snapshot.get("open_orders")
            if (
                not isinstance(summary, list)
                or not isinstance(positions, list)
                or not isinstance(open_orders, list)
            ):
                raise ValueError("snapshot account summary, positions, and open orders must be lists")

            cash_rows = []
            summary_tags: set[str] = set()
            for row in summary:
                if not isinstance(row, Mapping):
                    raise ValueError("account summary row must be a mapping")
                if (
                    row.get("snapshot_id") != snapshot_id
                    or row.get("account") != account_ref
                ):
                    raise ValueError("account summary row scope differs from snapshot")
                tag = row.get("tag")
                if tag not in {"NetLiquidation", "TotalCashValue", "AvailableFunds"}:
                    raise ValueError("snapshot contains an unsupported account summary tag")
                if row.get("currency") != "USD":
                    raise ValueError("snapshot account summary must use USD")
                if tag in summary_tags:
                    raise ValueError("snapshot repeats an account summary tag")
                summary_tags.add(tag)
                self._snapshot_decimal(row.get("value"), f"{tag} value")
                if tag == "TotalCashValue":
                    cash_rows.append(row)
            if summary_tags != {"NetLiquidation", "TotalCashValue", "AvailableFunds"} or len(cash_rows) != 1:
                raise ValueError("snapshot must contain exactly one of each required USD account summary tag")
            cash = self._snapshot_decimal(cash_rows[0].get("value"), "TotalCashValue")
            cash_value = float(cash)
            if not math.isfinite(cash_value):
                raise ValueError("TotalCashValue is outside the supported numeric range")

            if not isinstance(data_by_con_id, Mapping):
                raise ValueError("data_by_con_id must map contract ids to Backtrader feeds")
            data_ids: set[int] = set()
            for contract_id, data in data_by_con_id.items():
                if type(contract_id) is not int or contract_id <= 0 or data is None:
                    raise ValueError("contract mapping requires positive integer ids and data feeds")
                identity = id(data)
                if identity in data_ids:
                    raise ValueError("one Backtrader feed cannot map to multiple contract ids")
                data_ids.add(identity)

            restored_positions: dict[Any, Position] = {}
            seen_contract_ids: set[int] = set()
            for row in positions:
                if not isinstance(row, Mapping):
                    raise ValueError("position row must be a mapping")
                contract_id = row.get("contract_id")
                if type(contract_id) is not int or contract_id <= 0:
                    raise ValueError("position contract_id must be a positive integer")
                if contract_id in seen_contract_ids:
                    raise ValueError("snapshot repeats a contract position")
                seen_contract_ids.add(contract_id)
                if (
                    row.get("snapshot_id") != snapshot_id
                    or row.get("account") != account_ref
                    or row.get("currency") != "USD"
                ):
                    raise ValueError("position row scope or currency differs from snapshot")
                if not isinstance(row.get("symbol"), str) or not row["symbol"].strip():
                    raise ValueError("position symbol is required")
                if contract_id not in data_by_con_id:
                    raise ValueError(f"snapshot position conId {contract_id} has no Backtrader feed mapping")
                quantity = self._snapshot_decimal(row.get("quantity"), "position quantity")
                average_cost = self._snapshot_decimal(row.get("average_cost"), "position average cost")
                if quantity < 0 or quantity != quantity.to_integral_value():
                    raise ValueError("position quantity must be non-negative whole shares")
                if average_cost < 0:
                    raise ValueError("position average cost must be non-negative")
                quantity_value = int(quantity)
                cost_value = float(average_cost)
                if not math.isfinite(cost_value):
                    raise ValueError("position average cost is outside the supported numeric range")
                data = data_by_con_id[contract_id]
                restored_positions[data] = Position(size=quantity_value, price=cost_value)

            # Commit only after validating every field and mapping, so errors
            # cannot leave a partially hydrated broker.
            self._cash = cash_value
            self.startingcash = cash_value
            self._positions = defaultdict(Position, restored_positions)
            self._snapshot_hydrated = True

    def _assert_hydration_idle(self) -> None:
        if self._start_called:
            raise RuntimeError("account snapshot hydration must happen before broker start")
        if (
            self.orders
            or self._seen_event_ids
            or not self._callback_queue.empty()
            or self._notifications
            or self._callback_error is not None
        ):
            raise RuntimeError("account snapshot hydration requires no prior orders or broker callbacks")
        if self._snapshot_hydrated:
            raise RuntimeError("account snapshot has already hydrated this broker")

    def restore_reconciled_order(
        self,
        state: ReconciledOrderState,
        *,
        data: bt.LineSeries,
        owner: Any = None,
    ) -> bt.Order:
        """Rebuild one broker-bound BUY as a native order without resubmitting.

        The caller supplies strict host/ledger facts only after the matching
        broker order and PAPER account snapshot have reconciled. Account cash
        and positions must already have been hydrated from that snapshot. The
        persisted Backtrader ref is retained so durable callback mappings keep
        resolving; repeated/ambiguous refs and unsupported SELL recovery fail
        before modifying the registry.
        """
        if not isinstance(state, ReconciledOrderState):
            raise TypeError("state must be a validated ReconciledOrderState")
        with self._lifecycle_lock:
            if self._start_called:
                raise RuntimeError("order recovery must happen before broker start")
            if not self._snapshot_hydrated:
                raise RuntimeError("order recovery requires a hydrated reconciled account snapshot")
            if self._callback_error is not None or not self._callback_queue.empty():
                raise RuntimeError("order recovery requires an empty, healthy callback queue")
            if getattr(data, "_name", None) != state.instrument:
                raise ValueError("recovered order instrument differs from its Backtrader feed")
            if state.order_ref in self.orders:
                raise ValueError("recovered Backtrader order ref is already registered")

            position = self._positions.get(data, Position())
            if (
                int(position.size) != state.position_size_after
                or not math.isclose(
                    float(position.price), state.position_average_price_after,
                    rel_tol=0.0, abs_tol=1e-9,
                )
            ):
                raise ValueError("recovered order position differs from the hydrated broker snapshot")

            order = bt.BuyOrder(
                owner=owner,
                data=data,
                size=state.requested_qty,
                exectype=bt.Order.Market,
                simulated=True,
                price=state.reference_price,
            )
            # Order.ref is an instance field in the locked Backtrader version.
            # Retaining the durable ref keeps callback and cancel bindings exact.
            order.ref = state.order_ref
            order.comminfo = self.getcommissioninfo(data)
            order.submit(self)
            if state.status is not CallbackStatus.SUBMITTED:
                order.accept(self)
            if state.filled_qty:
                prior_size = state.position_size_after - state.filled_qty
                prior_notional = (
                    state.position_size_after * state.position_average_price_after
                    - sum(fill.quantity * fill.price for fill in state.fills)
                )
                if prior_size < 0 or (prior_size == 0 and not math.isclose(prior_notional, 0.0, abs_tol=1e-7)):
                    raise ValueError("reconciled per-fill facts do not match the pre-existing position")
                if prior_size and prior_notional <= 0:
                    raise ValueError("reconciled per-fill facts imply an invalid pre-existing cost basis")
                running_size = prior_size
                running_notional = prior_notional
                for fill in state.fills:
                    running_size += fill.quantity
                    running_notional += fill.quantity * fill.price
                    running_average = running_notional / running_size
                    filled_value = fill.quantity * fill.price
                    order.execute(
                        bt.date2num(fill.occurred_at.replace(tzinfo=None)),
                        fill.quantity,
                        fill.price,
                        0,
                        0.0,
                        0.0,
                        fill.quantity,
                        filled_value,
                        fill.commission,
                        order.comminfo.margin,
                        0.0,
                        running_size,
                        running_average,
                    )
                order.addcomminfo(order.comminfo)
            expected_status = {
                CallbackStatus.SUBMITTED: order.Submitted,
                CallbackStatus.ACCEPTED: order.Accepted,
                CallbackStatus.PARTIALLY_FILLED: order.Partial,
                CallbackStatus.FILLED: order.Completed,
            }[state.status]
            if order.status != expected_status:
                raise RuntimeError("Backtrader reconstructed an unexpected native order status")
            self._register_order(order)
            self._next_order_ref = max(self._next_order_ref, state.order_ref + 1)
            return order

    @staticmethod
    def _snapshot_decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be decimal") from exc
        if not result.is_finite():
            raise ValueError(f"{label} must be finite")
        return result

    def stop(self) -> None:
        stop = getattr(self.port, "stop", None)
        if stop is not None:
            stop()

    def on_callback(self, callback: BrokerCallback) -> None:
        """Enqueue an SDK-thread callback without touching Backtrader state."""
        if not isinstance(callback, BrokerCallback):
            callback = BrokerCallback(**callback)
        try:
            self._callback_queue.put_nowait(callback)
        except queue.Full as exc:
            error = CallbackQueueFull("broker callback queue is full; execution must pause")
            self._callback_error = error
            raise error from exc

    def getcash(self) -> float:
        return self._cash

    def getvalue(self, datas=None, mkt=False, lever=False) -> float:
        selected = self._positions if datas is None else {
            data: self._positions[data] for data in datas
        }
        position_value = 0.0
        for data, position in selected.items():
            size = float(position.size)
            if size == 0:
                continue
            try:
                price = float(data.close[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "position market value is unavailable until its Backtrader feed has a bar"
                ) from exc
            if not math.isfinite(price) or price <= 0:
                raise RuntimeError("position market value requires a finite positive feed close")
            position_value += size * price
        return self._cash + position_value

    def getposition(self, data) -> Position:
        return self._positions[data]

    def get_notification(self):
        return self._notifications.popleft() if self._notifications else None

    def notify(self, order: bt.Order) -> None:
        self._notifications.append(order.clone())

    def buy(
        self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
        tradeid=0, oco=None, trailamount=None, trailpercent=None, **kwargs,
    ):
        return self._create_order(
            bt.BuyOrder, owner, data, size, price, plimit, exectype, valid,
            tradeid, oco, trailamount, trailpercent, **kwargs,
        )

    def sell(
        self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
        tradeid=0, oco=None, trailamount=None, trailpercent=None, **kwargs,
    ):
        return self._create_order(
            bt.SellOrder, owner, data, size, price, plimit, exectype, valid,
            tradeid, oco, trailamount, trailpercent, **kwargs,
        )

    def _create_order(
        self, order_cls, owner, data, size, price, plimit, exectype, valid,
        tradeid, oco, trailamount, trailpercent, **kwargs,
    ):
        if not math.isfinite(float(size)) or int(size) != size or size == 0:
            raise ValueError("V1 broker orders require a nonzero whole-share quantity")
        order = order_cls(
            owner=owner, data=data, size=size, price=price, pricelimit=plimit,
            exectype=exectype, valid=valid, tradeid=tradeid, oco=oco,
            trailamount=trailamount, trailpercent=trailpercent, **kwargs,
        )
        order.ref = self._allocate_order_ref()
        order.comminfo = self.getcommissioninfo(data)
        self._register_order(order)
        command = self._command(order)
        try:
            # Trusted port binds identity and applies current authorization/risk.
            self.port.submit_order(command)
        except PortOrderRejected as exc:
            order.addinfo(dispatch_rejection=str(exc))
            order.reject(self)
            self.notify(order)
            return order
        except Exception as exc:
            self._mark_submission_uncertain(order, exc)
        return order

    def submit(self, order):
        if order.ref in self.orders:
            return order
        order.ref = self._allocate_order_ref()
        self._register_order(order)
        order.comminfo = self.getcommissioninfo(order.data)
        try:
            self.port.submit_order(self._command(order))
        except PortOrderRejected:
            order.reject(self)
            self.notify(order)
        except Exception as exc:
            self._mark_submission_uncertain(order, exc)
        return order

    def _register_order(self, order: bt.Order) -> None:
        ref = order.ref
        if type(ref) is not int or ref <= 0:
            raise ValueError("Backtrader order ref must be a positive integer")
        existing = self.orders.get(ref)
        if existing is not None and existing is not order:
            raise RuntimeError(f"duplicate Backtrader order ref {ref}")
        self.orders[ref] = order

    def _allocate_order_ref(self) -> int:
        """Return a unique monotonically increasing ref within this broker."""
        candidate = self._next_order_ref
        while candidate in self.orders:
            candidate += 1
        if candidate >= _MAX_ORDER_REF:
            raise RuntimeError("broker-local Backtrader order reference space is exhausted")
        self._next_order_ref = candidate + 1
        return candidate

    def _mark_submission_uncertain(self, order: bt.Order, cause: Exception) -> None:
        error = BrokerSubmissionUncertain(
            f"submission outcome for order {order.ref} is unknown; stop before retrying"
        )
        if order.status == order.Created:
            order.submit(self)
            self.notify(order)
        self._callback_error = error
        raise error from cause

    def cancel(self, order, bracket=False):
        if order.ref not in self.orders or order.status in (
            order.Completed, order.Canceled, order.Expired, order.Rejected, order.Margin,
        ):
            return False
        try:
            self.port.cancel_order(self._command(order))
            return True
        except PortOrderRejected:
            return False
        except Exception as exc:
            error = BrokerCommandUncertain(
                f"cancel outcome for order {order.ref} is unknown; stop before retrying"
            )
            self._callback_error = error
            raise error from exc

    @staticmethod
    def _command(order: bt.Order) -> BrokerOrderCommand:
        if order.exectype not in (None, order.Market):
            raise ValueError("V1 broker proxy supports market orders only")
        if order.valid not in (None, bt.Order.DAY):
            raise ValueError("V1 broker proxy supports DAY orders only")
        if order.oco is not None or order.parent is not None:
            raise ValueError("V1 broker proxy does not support linked orders")
        if order.trailamount is not None or order.trailpercent is not None:
            raise ValueError("V1 broker proxy does not support trailing orders")
        return BrokerOrderCommand(
            order_ref=order.ref,
            instrument=order.data._name or str(order.data),
            side="BUY" if order.isbuy() else "SELL",
            quantity=abs(int(order.created.size)),
        )

    def next(self) -> None:
        """Pump callbacks on Cerebro's thread, including no-new-bar polls."""
        if self._callback_error is not None:
            raise self._callback_error
        process_callbacks = getattr(self.port, "process_callbacks", None)
        if process_callbacks is not None:
            # A durable callback pump may feed this broker synchronously. It
            # must run on Cerebro's thread before native orders are mutated.
            process_callbacks()
        while True:
            try:
                event = self._callback_queue.get_nowait()
            except queue.Empty:
                break
            previous = self._seen_event_ids.get(event.event_id)
            if previous is not None:
                if previous != event:
                    error = RuntimeError(
                        f"callback event_id {event.event_id!r} was reused with different content"
                    )
                    self._callback_error = error
                    raise error
                # A durable pump can replay an event after the broker applied
                # it but before its ACK committed. Identical duplicates are
                # safe to ACK again; otherwise the inbox row would remain
                # pending forever after a partial crash/restart.
                acknowledge = getattr(self.port, "acknowledge_callback", None)
                if acknowledge is not None:
                    acknowledge(event.event_id)
                continue
            self._seen_event_ids[event.event_id] = event
            try:
                self._apply_callback(event)
                acknowledge = getattr(self.port, "acknowledge_callback", None)
                if acknowledge is not None:
                    acknowledge(event.event_id)
            except Exception as exc:
                self._callback_error = exc
                raise
        self._dispatch_recovered_intents()
        if self._on_broker_cycle is not None:
            self._on_broker_cycle()

    def _dispatch_recovered_intents(self) -> None:
        if not self._recovered_intents or self._recovered_dispatch_ready is None:
            return
        if not self._recovered_dispatch_ready():
            return
        while self._recovered_intents:
            data, instrument, side, quantity, reference_price, intent_id = self._recovered_intents[0]
            bind = getattr(self.port, "bind_recovered_intent", None)
            if bind is None:
                raise RuntimeError("trusted port does not support recovered intent binding")
            with bind(intent_id):
                order = self.buy(
                    owner=None, data=data, size=quantity, price=reference_price, simulated=True,
                ) if side == "BUY" else self.sell(
                    owner=None, data=data, size=quantity, price=reference_price, simulated=True,
                )
            if order.status == order.Rejected:
                raise PortOrderRejected(
                    f"recovered intent {intent_id} was rejected: "
                    f"{order.info.get('dispatch_rejection', 'current authorization or risk checks')}"
                )
            self._recovered_intents.popleft()
            self._recovered_intent_ids.discard(intent_id)

    def _apply_callback(self, event: BrokerCallback) -> None:
        order = self.orders.get(event.order_ref)
        if order is None:
            raise RuntimeError(f"callback references unknown order_ref {event.order_ref}")

        status = event.status
        if event.filled_qty < abs(int(order.executed.size)):
            raise RuntimeError("cumulative fill quantity moved backwards")
        if event.filled_qty > abs(int(order.created.size)):
            raise RuntimeError("cumulative fill quantity exceeds requested quantity")
        requested_qty = abs(int(order.created.size))
        if status is CallbackStatus.PARTIALLY_FILLED and event.filled_qty >= requested_qty:
            raise RuntimeError("partial-fill callback filled the entire order")
        if status is CallbackStatus.FILLED and event.filled_qty != requested_qty:
            raise RuntimeError("filled callback quantity differs from order quantity")

        if event.filled_qty > abs(int(order.executed.size)):
            if event.average_fill_price is None:
                raise RuntimeError("fill callback has no average price")
            # IB can deliver execDetails before an orderStatus callback. Move
            # the native order through the broker-confirmed lifecycle before
            # applying that first cumulative fill.
            if order.status == order.Created:
                order.submit(self)
                self.notify(order)
            if order.status == order.Submitted:
                order.accept(self)
                self.notify(order)
            self._apply_fill(order, event)

        if status is CallbackStatus.SUBMITTED:
            if order.status == order.Created:
                order.submit(self)
            else:
                return
        elif status is CallbackStatus.ACCEPTED:
            if order.status == order.Created:
                order.submit(self)
            if order.status == order.Submitted:
                order.accept(self)
            else:
                return
        elif status is CallbackStatus.PARTIALLY_FILLED:
            pass
        elif status is CallbackStatus.FILLED:
            pass
        elif status is CallbackStatus.CANCELLED:
            if order.status in (order.Completed, order.Rejected, order.Expired, order.Margin):
                return
            order.cancel()
        elif status is CallbackStatus.REJECTED:
            if order.status in (order.Completed, order.Canceled, order.Expired, order.Margin):
                return
            order.reject(self)
        elif status is CallbackStatus.EXPIRED:
            if order.status in (order.Completed, order.Canceled, order.Rejected, order.Margin):
                return
            order.expire()
        self.notify(order)

    def _apply_fill(self, order: bt.Order, event: BrokerCallback) -> None:
        prior_qty = abs(int(order.executed.size))
        cumulative_qty = event.filled_qty
        delta_abs = cumulative_qty - prior_qty
        delta = delta_abs if order.isbuy() else -delta_abs
        # SDK callbacks commonly carry cumulative average price. Recover the
        # price of only the newly reported quantity so BT's executed average
        # remains equal to the broker's cumulative average.
        prior_avg = float(order.executed.price) if prior_qty else 0.0
        price = (
            cumulative_qty * float(event.average_fill_price) - prior_qty * prior_avg
        ) / delta_abs
        if not math.isfinite(price) or price <= 0:
            raise RuntimeError("cumulative average implies an invalid incremental fill price")
        data = order.data
        position = self._positions[data]
        prior_price = position.price
        if order.issell() and position.size + delta < 0:
            # V1 strategy contract is long-only; the trusted port should reject
            # this before dispatch, and this is a second fail-closed check.
            raise RuntimeError("sell callback would create a short position")

        comminfo = self.getcommissioninfo(data)
        cumulative_commission = float(event.cumulative_commission)
        previous_commission = float(order.executed.comm)
        commission_delta = cumulative_commission - previous_commission
        if commission_delta < -1e-9:
            raise RuntimeError("cumulative commission moved backwards")
        commission_delta = max(0.0, commission_delta)

        new_size, new_price, opened, closed = position.update(delta, price, event.occurred_at)

        signed_closed = closed
        pnl = comminfo.profitandloss(-signed_closed, prior_price, price) if signed_closed else 0.0
        self._cash -= delta * price
        self._cash -= commission_delta
        closed_value = abs(closed) * prior_price if closed else 0.0
        opened_value = abs(opened) * price if opened else 0.0
        closed_comm = commission_delta * (abs(closed) / delta_abs) if closed else 0.0
        opened_comm = commission_delta - closed_comm
        order.execute(
            bt.date2num(event.occurred_at.replace(tzinfo=None)),
            delta,
            price,
            closed,
            closed_value,
            closed_comm,
            opened,
            opened_value,
            opened_comm,
            comminfo.margin,
            pnl,
            new_size,
            new_price,
        )
        order.addcomminfo(comminfo)
