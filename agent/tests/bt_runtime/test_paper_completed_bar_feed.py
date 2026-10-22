from __future__ import annotations

import datetime as dt
from decimal import Decimal

import backtrader as bt
import pytest

from src.bt_runtime.brokers import BrokerCallback, CallbackStatus, PortBroker
from src.bt_runtime.feeds import PaperCompletedBarFeed, PaperFeedError
from src.execution.contracts import Bar


NOW = dt.datetime(2025, 1, 7, 22, 0, tzinfo=dt.timezone.utc)


def _bar(day: int, *, final=True, instrument="AAPL", available_at=None):
    session = dt.date(2025, 1, day)
    end = dt.datetime(2025, 1, day, 21, 0, tzinfo=dt.timezone.utc)
    return Bar(
        instrument_id=instrument,
        session_date=session,
        start_at=end - dt.timedelta(hours=6, minutes=30),
        end_at=end,
        available_at=available_at or end + dt.timedelta(minutes=1),
        timezone="America/New_York",
        open=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10"),
        volume=Decimal("100"), currency="USD", is_final=final,
        source="host_fixture", adjustment="split_adjusted",
    )


class CallbackPort:
    def __init__(self):
        self.sink = None
        self.submitted = []

    def register_callback_sink(self, sink):
        self.sink = sink

    def submit_order(self, command):
        self.submitted.append(command)
        self.emit(command.order_ref, CallbackStatus.SUBMITTED, "submitted")
        self.emit(command.order_ref, CallbackStatus.ACCEPTED, "accepted")

    def cancel_order(self, command):
        raise AssertionError("test does not cancel")

    def emit(self, order_ref, status, suffix, *, filled=0, price=None):
        self.sink(BrokerCallback(
            order_ref=order_ref,
            status=status,
            event_id=f"{order_ref}:{suffix}",
            occurred_at=dt.datetime(2025, 1, 3, 21, 1, tzinfo=dt.timezone.utc),
            filled_qty=filled,
            average_fill_price=price,
        ))


class OneOrderThenWait(bt.Strategy):
    def __init__(self):
        self.order = None
        self.statuses = []
        self.bar_count_at_notify = []

    def next(self):
        if self.order is None:
            self.order = self.buy(size=1)

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())
        self.bar_count_at_notify.append(len(self.data))
        if order.status == order.Accepted:
            self.env.broker.port.emit(
                order.ref, CallbackStatus.FILLED, "filled", filled=1, price=10.5,
            )
        elif order.status == order.Completed:
            self.env.runstop()


def test_feed_rejects_partial_future_wrong_symbol_and_nonmonotonic_bars():
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: NOW, idle_poll_seconds=0.01,
    )
    with pytest.raises(PaperFeedError, match="partial bars"):
        feed.push_completed_bar(_bar(2, final=False))
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: NOW, idle_poll_seconds=0.01,
    )
    with pytest.raises(PaperFeedError, match="not yet available"):
        feed.push_completed_bar(_bar(2, available_at=NOW + dt.timedelta(seconds=1)))
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: NOW, idle_poll_seconds=0.01,
    )
    with pytest.raises(PaperFeedError, match="instrument"):
        feed.push_completed_bar(_bar(2, instrument="MSFT"))

    ordered = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    ordered.push_completed_bar(_bar(3))
    with pytest.raises(PaperFeedError, match="strictly increasing"):
        ordered.push_completed_bar(_bar(2))


def test_feed_rejects_weekend_and_bars_outside_the_scheduled_xnys_session():
    weekend_feed = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    with pytest.raises(PaperFeedError, match="not a scheduled XNYS session"):
        weekend_feed.push_completed_bar(_bar(6).model_copy(update={"session_date": dt.date(2025, 1, 4)}))

    outside_feed = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    late_close = _bar(6).model_copy(update={
        "end_at": dt.datetime(2025, 1, 6, 22, 0, tzinfo=dt.timezone.utc),
        "available_at": dt.datetime(2025, 1, 6, 22, 1, tzinfo=dt.timezone.utc),
    })
    with pytest.raises(PaperFeedError, match="scheduled XNYS close"):
        outside_feed.push_completed_bar(late_close)


def test_feed_uses_the_scheduled_early_close_for_half_day_sessions():
    close = dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.timezone.utc)
    early_bar = Bar(
        instrument_id="AAPL", session_date=dt.date(2025, 11, 28),
        start_at=dt.datetime(2025, 11, 28, 14, 30, tzinfo=dt.timezone.utc),
        end_at=close, available_at=close + dt.timedelta(minutes=1),
        timezone="America/New_York", open=Decimal("10"), high=Decimal("11"),
        low=Decimal("9"), close=Decimal("10"), volume=Decimal("100"),
        currency="USD", is_final=True, source="host_fixture", adjustment="split_adjusted",
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: close + dt.timedelta(minutes=2),
    )
    feed.push_completed_bar(early_bar)

    full_day_bar = early_bar.model_copy(update={
        "end_at": dt.datetime(2025, 11, 28, 21, 0, tzinfo=dt.timezone.utc),
        "available_at": dt.datetime(2025, 11, 28, 21, 1, tzinfo=dt.timezone.utc),
    })
    late_feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: dt.datetime(2025, 11, 28, 21, 2, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PaperFeedError, match="scheduled XNYS close"):
        late_feed.push_completed_bar(full_day_bar)


def test_queue_overflow_latches_feed_failure_and_invokes_pause_sink():
    pauses = []
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: NOW, queue_size=1, failure_sink=pauses.append,
    )
    feed.push_completed_bar(_bar(2))
    with pytest.raises(PaperFeedError, match="queue is full"):
        feed.push_completed_bar(_bar(3))
    assert feed.failure is not None
    assert pauses == [feed.failure]
    with pytest.raises(PaperFeedError, match="queue is full"):
        feed.push_completed_bar(_bar(6))


def test_real_cerebro_receives_broker_fill_during_live_feed_idle_poll_without_new_bar():
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", now=lambda: NOW, idle_poll_seconds=0.01,
    )
    feed.push_completed_bar(_bar(2))
    port = CallbackPort()
    broker = PortBroker(port, initial_cash=100)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False, preload=False, runonce=False)
    cerebro.adddata(feed, name="AAPL")
    cerebro.setbroker(broker)
    cerebro.addstrategy(OneOrderThenWait)

    strategy = cerebro.run()[0]

    assert len(port.submitted) == 1
    assert strategy.statuses[-2:] == ["Accepted", "Completed"]
    assert len(strategy.statuses) == 3  # Submitted, Accepted, Completed
    assert strategy.bar_count_at_notify == [1, 1, 1]
    assert len(strategy.data) == 1  # order/fill callbacks arrived during idle, with no second bar
    assert strategy.order.executed.size == 1
    assert strategy.order.executed.price == pytest.approx(10.5)
    assert broker.getposition(feed).size == 1
    assert broker.getcash() == pytest.approx(89.5)
