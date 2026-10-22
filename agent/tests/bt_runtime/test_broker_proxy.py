from __future__ import annotations

import datetime as dt
import threading
from dataclasses import replace

import backtrader as bt
import pytest

from src.bt_runtime.brokers import (
    BrokerCallback,
    BrokerCommandUncertain,
    BrokerSubmissionUncertain,
    CallbackQueueFull,
    CallbackStatus,
    PortOrderRejected,
    PortBroker,
    ReconciledFill,
    ReconciledOrderState,
)


class IdleFeed(bt.feed.DataBase):
    params = (("idle_polls", 8),)

    def __init__(self):
        super().__init__()
        self.sent = 0
        self.idles = 0
        self._idle_count = 0

    def islive(self):
        return True

    def _load(self):
        if self.sent and self._idle_count < self.p.idle_polls:
            self._idle_count += 1
            self.idles += 1
            return None
        if self.sent >= 2:
            return False
        day = self.sent + 2
        self.sent += 1
        self._idle_count = 0
        self.lines.datetime[0] = bt.date2num(dt.datetime(2025, 1, day, 9, 30))
        self.lines.open[0] = self.lines.high[0] = 10.0
        self.lines.low[0] = self.lines.close[0] = 10.0
        self.lines.volume[0] = 100
        self.lines.openinterest[0] = 0
        return True


class FakeTrustedPort:
    """Fake port: every order/cancel command crosses the trust boundary."""

    def __init__(self):
        self.sink = None
        self.submitted = []
        self.cancelled = []

    def register_callback_sink(self, sink):
        self.sink = sink

    def submit_order(self, command):
        self.submitted.append(command)
        self.emit(command.order_ref, CallbackStatus.SUBMITTED)
        self.emit(command.order_ref, CallbackStatus.ACCEPTED)

    def cancel_order(self, command):
        self.cancelled.append(command)
        self.emit(command.order_ref, CallbackStatus.CANCELLED)

    def emit(self, order_ref, status, *, filled=0, price=None, commission=0.0, event_id=None):
        assert self.sink is not None
        self.sink(BrokerCallback(
            order_ref=order_ref,
            status=status,
            event_id=event_id or f"event-{order_ref}-{status}-{filled}",
            occurred_at=dt.datetime(2025, 1, 2, 14, 31, tzinfo=dt.timezone.utc),
            filled_qty=filled,
            average_fill_price=price,
            cumulative_commission=commission,
        ))


class PumpStrategy(bt.Strategy):
    def __init__(self):
        self.events = []
        self.orders = []
        self.cancelled_once = False
        self.submitted_bar = None

    def next(self):
        if not self.orders:
            self.submitted_bar = len(self.data)
            self.orders = [self.buy(size=2), self.buy(size=1)]

    def notify_order(self, order):
        self.events.append((order.ref, order.getstatusname(), len(self.data), isinstance(order, bt.Order)))
        if order.ref == self.orders[1].ref and order.status == order.Accepted and not self.cancelled_once:
            self.cancelled_once = self.cancel(self.orders[1])
        if order.ref == self.orders[0].ref and order.status == order.Accepted:
            self.env.broker.port.emit(order.ref, CallbackStatus.PARTIALLY_FILLED, filled=1, price=10.0)
        if order.ref == self.orders[0].ref and order.status == order.Partial:
            self.env.broker.port.emit(order.ref, CallbackStatus.FILLED, filled=2, price=11.0, commission=0.2)
        statuses = {ref: status for ref, status, _, _ in self.events}
        if statuses.get(self.orders[0].ref) == "Completed" and statuses.get(self.orders[1].ref) == "Canceled":
            self.env.runstop()


def test_real_cerebro_pumps_async_callbacks_during_empty_polls_and_maps_native_orders():
    port = FakeTrustedPort()
    broker = PortBroker(port, initial_cash=1_000, callback_queue_size=8)
    feed = IdleFeed(idle_polls=8)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    cerebro.setbroker(broker)
    cerebro.addstrategy(PumpStrategy)

    strategy = cerebro.run()[0]

    assert len(port.submitted) == 2
    assert [command.side for command in port.submitted] == ["BUY", "BUY"]
    assert len(port.cancelled) == 1
    assert port.cancelled[0].order_ref == strategy.orders[1].ref
    assert {status for _, status, _, _ in strategy.events} >= {"Accepted", "Partial", "Completed", "Canceled"}
    assert all(native for *_, native in strategy.events)
    assert {bar for _, _, bar, _ in strategy.events} == {strategy.submitted_bar}
    assert feed.idles > 0
    assert broker.getposition(feed).size == 2
    assert strategy.orders[0].executed.price == pytest.approx(11.0)
    assert broker.getcash() == pytest.approx(977.8)


def _account_snapshot(*, position_size, average_price, cash):
    return {
        "snapshot_id": 81,
        "account_ref": "paper-fixture",
        "environment": "paper",
        "capture_complete": True,
        "reconciled": True,
        "requested_at_utc": "2026-01-01T00:00:00+00:00",
        "completed_at_utc": "2026-01-01T00:00:01+00:00",
        "account_summary": [
            {"snapshot_id": 81, "account": "paper-fixture", "tag": "NetLiquidation", "currency": "USD", "value": "1000"},
            {"snapshot_id": 81, "account": "paper-fixture", "tag": "TotalCashValue", "currency": "USD", "value": str(cash)},
            {"snapshot_id": 81, "account": "paper-fixture", "tag": "AvailableFunds", "currency": "USD", "value": "900"},
        ],
        "positions": ([{
            "snapshot_id": 81, "account": "paper-fixture", "contract_id": 901,
            "symbol": "SYNTH", "currency": "USD", "quantity": str(position_size),
            "average_cost": str(average_price),
        }] if position_size else []),
        "open_orders": [],
        "reconciliation_result": {
            "snapshot_id": 81,
            "account_ref": "paper-fixture",
            "environment": "paper",
            "checks": ["scope", "open_orders", "positions", "order_id_high_water_mark"],
        },
    }


def _one_fill(quantity, price, occurred_at, commission=0.0):
    return (ReconciledFill(
        execution_id="fixture-exec-1", quantity=quantity, price=price,
        occurred_at=occurred_at, commission=commission,
    ),)


@pytest.mark.parametrize(
    ("status", "filled", "expected_native_status"),
    [
        (CallbackStatus.PARTIALLY_FILLED, 2, bt.Order.Partial),
        (CallbackStatus.FILLED, 5, bt.Order.Completed),
    ],
)
def test_rebuilds_reconciled_native_buy_order_without_resubmission_and_applies_callback(
    status, filled, expected_native_status,
):
    port = FakeTrustedPort()
    broker = PortBroker(port, initial_cash=1_000)
    feed = IdleFeed(idle_polls=4)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    broker.hydrate_reconciled_account_snapshot(
        _account_snapshot(position_size=filled, average_price=11.0, cash=1_000 - filled * 11.0 - 0.5),
        account_ref="paper-fixture",
        environment="paper",
        data_by_con_id={901: feed},
    )
    occurred_at = dt.datetime(2026, 1, 2, 15, 0, tzinfo=dt.timezone.utc)
    state = ReconciledOrderState(
        order_ref=77,
        instrument="SYNTH",
        side="BUY",
        requested_qty=5,
        reference_price=10.0,
        status=status,
        filled_qty=filled,
        average_fill_price=11.0,
        cumulative_commission=0.5,
        occurred_at=occurred_at,
        position_size_after=filled,
        position_average_price_after=11.0,
        fills=_one_fill(filled, 11.0, occurred_at, 0.5),
    )

    order = broker.restore_reconciled_order(state, data=feed)

    assert isinstance(order, bt.Order)
    assert order.ref == 77
    assert order.status == expected_native_status
    assert order.created.size == 5
    assert order.executed.size == filled
    assert order.executed.price == pytest.approx(11.0)
    assert order.executed.comm == pytest.approx(0.5)
    assert port.submitted == []
    assert broker.getposition(feed).size == filled
    assert broker.getcash() == pytest.approx(1_000 - filled * 11.0 - 0.5)

    broker.on_callback(BrokerCallback(
        order_ref=77,
        status=status,
        event_id=f"replayed-{status.value}",
        occurred_at=occurred_at,
        filled_qty=filled,
        average_fill_price=11.0,
        cumulative_commission=0.5,
    ))
    broker.next()

    notification = broker.get_notification()
    assert notification is not None
    assert notification.ref == 77
    assert notification.status == expected_native_status
    assert notification.executed.size == filled
    assert broker.getposition(feed).size == filled
    assert broker.getcash() == pytest.approx(1_000 - filled * 11.0 - 0.5)
    assert port.submitted == []


def test_order_recovery_rejects_unverified_or_ambiguous_state_without_registry_mutation():
    broker = PortBroker(FakeTrustedPort(), initial_cash=1_000)
    feed = IdleFeed(idle_polls=0)
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    state = ReconciledOrderState(
        order_ref=9, instrument="SYNTH", side="BUY", requested_qty=3,
        reference_price=10.0, status=CallbackStatus.PARTIALLY_FILLED,
        filled_qty=1, average_fill_price=10.0, cumulative_commission=0.0,
        occurred_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        position_size_after=1, position_average_price_after=10.0,
        fills=_one_fill(1, 10.0, dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)),
    )

    with pytest.raises(RuntimeError, match="hydrated reconciled account snapshot"):
        broker.restore_reconciled_order(state, data=feed)
    assert broker.orders == {}
    assert broker.getposition(feed).size == 0

    broker.hydrate_reconciled_account_snapshot(
        _account_snapshot(position_size=2, average_price=10.0, cash=980.0),
        account_ref="paper-fixture", environment="paper", data_by_con_id={901: feed},
    )
    with pytest.raises(ValueError, match="position differs"):
        broker.restore_reconciled_order(state, data=feed)
    assert broker.orders == {}

    with pytest.raises(ValueError, match="only BUY"):
        ReconciledOrderState(
            order_ref=10, instrument="SYNTH", side="SELL", requested_qty=2,
            reference_price=10.0, status=CallbackStatus.FILLED,
            filled_qty=2, average_fill_price=10.0, cumulative_commission=0.0,
            occurred_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
            position_size_after=0, position_average_price_after=0.0,
            fills=_one_fill(2, 10.0, dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)),
        )


def test_restored_order_callback_reaches_strategy_notify_order_with_its_native_ref():
    occurred_at = dt.datetime(2026, 1, 2, 15, 0, tzinfo=dt.timezone.utc)
    port = FakeTrustedPort()
    broker = PortBroker(port, initial_cash=1_000)
    feed = IdleFeed(idle_polls=4)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    broker.hydrate_reconciled_account_snapshot(
        _account_snapshot(position_size=2, average_price=11.0, cash=977.8),
        account_ref="paper-fixture", environment="paper", data_by_con_id={901: feed},
    )
    broker.restore_reconciled_order(ReconciledOrderState(
        order_ref=88, instrument="SYNTH", side="BUY", requested_qty=5,
        reference_price=10.0, status=CallbackStatus.PARTIALLY_FILLED,
        filled_qty=2, average_fill_price=11.0, cumulative_commission=0.2,
        occurred_at=occurred_at, position_size_after=2, position_average_price_after=11.0,
        fills=_one_fill(2, 11.0, occurred_at, 0.2),
    ), data=feed)
    cerebro.setbroker(broker)

    class Observer(bt.Strategy):
        def __init__(self):
            self.order_events = []

        def start(self):
            self.env.broker.on_callback(BrokerCallback(
                order_ref=88, status=CallbackStatus.PARTIALLY_FILLED,
                event_id="restored-partial-callback", occurred_at=occurred_at,
                filled_qty=2, average_fill_price=11.0, cumulative_commission=0.2,
            ))

        def notify_order(self, order):
            self.order_events.append((order.ref, order.status, order.executed.size))
            if order.ref == 88 and order.status == order.Partial:
                self.env.runstop()

    cerebro.addstrategy(Observer)
    strategy = cerebro.run()[0]

    assert (88, bt.Order.Partial, 2) in strategy.order_events
    assert port.submitted == []
    assert broker.getposition(feed).size == 2
    assert broker.getcash() == pytest.approx(977.8)


def test_restored_partial_buy_preserves_each_execution_bit_and_checks_aggregate():
    first_at = dt.datetime(2026, 1, 2, 15, 0, tzinfo=dt.timezone.utc)
    second_at = first_at + dt.timedelta(minutes=1)
    fills = (
        ReconciledFill("execution-1", 1, 101.0, first_at),
        ReconciledFill("execution-2", 1, 103.0, second_at),
    )
    state = ReconciledOrderState(
        order_ref=89, instrument="SYNTH", side="BUY", requested_qty=3,
        reference_price=100.0, status=CallbackStatus.PARTIALLY_FILLED,
        filled_qty=2, average_fill_price=102.0, cumulative_commission=0.0,
        occurred_at=second_at, position_size_after=3, position_average_price_after=98.0,
        fills=fills,
    )
    port = FakeTrustedPort()
    broker = PortBroker(port, initial_cash=1_000)
    feed = IdleFeed(idle_polls=0)
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    broker.hydrate_reconciled_account_snapshot(
        _account_snapshot(position_size=3, average_price=98.0, cash=706.0),
        account_ref="paper-fixture", environment="paper", data_by_con_id={901: feed},
    )

    restored = broker.restore_reconciled_order(state, data=feed)

    assert restored.executed.size == 2
    assert restored.executed.price == pytest.approx(102.0)
    assert [bit.price for bit in restored.executed.exbits] == [101.0, 103.0]
    assert [bt.num2date(bit.dt).minute for bit in restored.executed.exbits] == [0, 1]
    assert [(bit.psize, bit.pprice) for bit in restored.executed.exbits] == pytest.approx(
        [(2, 95.5), (3, 98.0)],
    )
    assert broker.getposition(feed).size == 3
    assert port.submitted == []

    with pytest.raises(ValueError, match="cumulative average fill price"):
        ReconciledOrderState(
            order_ref=90, instrument="SYNTH", side="BUY", requested_qty=3,
            reference_price=100.0, status=CallbackStatus.PARTIALLY_FILLED,
            filled_qty=2, average_fill_price=101.0, cumulative_commission=0.0,
            occurred_at=second_at, position_size_after=3, position_average_price_after=98.0,
            fills=fills,
        )
    with pytest.raises(ValueError, match="chronological order"):
        ReconciledOrderState(
            order_ref=91, instrument="SYNTH", side="BUY", requested_qty=3,
            reference_price=100.0, status=CallbackStatus.PARTIALLY_FILLED,
            filled_qty=2, average_fill_price=102.0, cumulative_commission=0.0,
            occurred_at=second_at, position_size_after=3, position_average_price_after=98.0,
            fills=tuple(reversed(fills)),
        )


def test_restored_ref_advances_broker_allocator_before_future_native_order_submission():
    port = FakeTrustedPort()
    broker = PortBroker(port, initial_cash=1_000)
    feed = IdleFeed(idle_polls=0)
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    broker.hydrate_reconciled_account_snapshot(
        _account_snapshot(position_size=0, average_price=0.0, cash=1_000.0),
        account_ref="paper-fixture", environment="paper", data_by_con_id={901: feed},
    )

    restored = broker.restore_reconciled_order(ReconciledOrderState(
        order_ref=1, instrument="SYNTH", side="BUY", requested_qty=2,
        reference_price=10.0, status=CallbackStatus.ACCEPTED,
        filled_qty=0, average_fill_price=None, cumulative_commission=0.0,
        occurred_at=None, position_size_after=0, position_average_price_after=0.0,
    ), data=feed)

    # This is a fresh-process collision scenario: Backtrader's own Order.ref
    # basis has no knowledge of the durable ref restored above.
    cerebro.setbroker(broker)

    class SubmitOneOrder(bt.Strategy):
        def next(self):
            if not hasattr(self, "created"):
                self.created = self.buy(size=1)

        def notify_order(self, order):
            if order.ref == 2 and order.status == order.Accepted:
                self.env.runstop()

    cerebro.addstrategy(SubmitOneOrder)
    strategy = cerebro.run()[0]
    created = strategy.created

    assert restored.ref == 1
    assert restored.ref in broker.orders
    assert created.ref == 2
    assert created is broker.orders[2]
    assert [command.order_ref for command in port.submitted] == [2]


def test_callback_thread_only_enqueues_and_duplicate_event_is_idempotent():
    port = FakeTrustedPort()
    acknowledgements = []
    port.acknowledge_callback = acknowledgements.append
    broker = PortBroker(port, initial_cash=100)
    # Use a real Backtrader order/data from a short Cerebro setup for the order registry.
    feed = IdleFeed(idle_polls=0)
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(feed, name="SYNTH")
    cerebro.setbroker(broker)
    cerebro.addstrategy(PumpStrategy)
    # Construct a native order against the attached feed without running Cerebro.
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10.0)
    order.comminfo = broker.getcommissioninfo(feed)
    broker.orders[order.ref] = order

    callback = BrokerCallback(
        order_ref=order.ref,
        status=CallbackStatus.ACCEPTED,
        event_id="accepted-1",
        occurred_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
    )
    worker = threading.Thread(target=broker.on_callback, args=(callback,))
    worker.start()
    worker.join()
    assert order.status == order.Created
    assert broker.get_notification() is None

    broker.next()
    assert acknowledgements == ["accepted-1"]
    broker.on_callback(callback)
    broker.next()
    assert acknowledgements == ["accepted-1", "accepted-1"]
    assert order.status == order.Accepted
    notification = broker.get_notification()
    assert notification is not None and notification.status == order.Accepted
    assert broker.get_notification() is None


def test_fill_callback_can_arrive_before_order_status_and_keeps_native_lifecycle_valid():
    broker = PortBroker(FakeTrustedPort(), initial_cash=100)
    feed = IdleFeed(idle_polls=0)
    order = bt.BuyOrder(owner=None, data=feed, size=2, simulated=True, price=10.0)
    broker._register_order(order)

    broker.on_callback(BrokerCallback(
        order_ref=order.ref,
        status=CallbackStatus.PARTIALLY_FILLED,
        event_id="fill-before-status",
        occurred_at=dt.datetime(2025, 1, 2, 14, 31, tzinfo=dt.timezone.utc),
        filled_qty=1,
        average_fill_price=10.0,
    ))
    broker.next()

    assert order.status == order.Partial
    notifications = []
    while (notification := broker.get_notification()) is not None:
        notifications.append(notification.status)
    assert notifications == [order.Submitted, order.Accepted, order.Partial]


def test_callback_queue_is_bounded_and_overflow_fails_closed():
    broker = PortBroker(FakeTrustedPort(), callback_queue_size=1)
    first = BrokerCallback(
        order_ref=1,
        status=CallbackStatus.ACCEPTED,
        event_id="one",
        occurred_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
    )
    second = BrokerCallback(
        order_ref=1,
        status=CallbackStatus.ACCEPTED,
        event_id="two",
        occurred_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
    )
    broker.on_callback(first)
    with pytest.raises(CallbackQueueFull):
        broker.on_callback(second)
    with pytest.raises(CallbackQueueFull):
        broker.next()


def test_uncertain_submit_is_not_reported_rejected_and_stops_dispatch():
    class UncertainPort(FakeTrustedPort):
        def submit_order(self, command):
            self.submitted.append(command)
            raise TimeoutError("transport timed out after send may have started")

    port = UncertainPort()
    broker = PortBroker(port, initial_cash=100)
    feed = IdleFeed(idle_polls=0)
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10.0)

    with pytest.raises(BrokerSubmissionUncertain):
        broker.submit(order)

    assert order.status == order.Submitted
    assert order.status != order.Rejected
    with pytest.raises(BrokerSubmissionUncertain):
        broker.next()


def test_only_explicit_pre_dispatch_rejection_maps_to_native_rejected():
    class RejectingPort(FakeTrustedPort):
        def submit_order(self, command):
            self.submitted.append(command)
            raise PortOrderRejected("risk gate denied before dispatch")

    port = RejectingPort()
    broker = PortBroker(port, initial_cash=100)
    feed = IdleFeed(idle_polls=0)
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10.0)

    broker.submit(order)

    assert order.status == order.Rejected
    assert broker.get_notification().status == order.Rejected


def test_callback_ref_validation_and_conflicting_event_id_fail_closed():
    with pytest.raises(ValueError, match="order_ref"):
        BrokerCallback(
            order_ref=True,
            status=CallbackStatus.ACCEPTED,
            event_id="invalid-ref",
            occurred_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
        )

    broker = PortBroker(FakeTrustedPort(), initial_cash=100)
    feed = IdleFeed(idle_polls=0)
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10.0)
    broker._register_order(order)
    first = BrokerCallback(
        order_ref=order.ref,
        status=CallbackStatus.ACCEPTED,
        event_id="event-reused",
        occurred_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
    )
    broker.on_callback(first)
    broker.next()
    broker.on_callback(replace(first, status=CallbackStatus.REJECTED))

    with pytest.raises(RuntimeError, match="reused with different content"):
        broker.next()
    with pytest.raises(RuntimeError, match="reused with different content"):
        broker.next()


def test_uncertain_cancel_is_not_reported_as_failed_cancel_and_stops_pump():
    class UncertainCancelPort(FakeTrustedPort):
        def cancel_order(self, command):
            self.cancelled.append(command)
            raise TimeoutError("cancel timed out after dispatch may have started")

    broker = PortBroker(UncertainCancelPort(), initial_cash=100)
    feed = IdleFeed(idle_polls=0)
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10.0)
    broker._register_order(order)
    order.submit(broker)

    with pytest.raises(BrokerCommandUncertain, match="cancel outcome"):
        broker.cancel(order)

    assert order.status == order.Submitted
    with pytest.raises(BrokerCommandUncertain, match="cancel outcome"):
        broker.next()
