"""Offline Cerebro/BrokerBase probe for notifications during empty live-feed polls.

This experiment uses only synthetic bars and a local fake callback queue. It
does not import ibapi, open a socket, read credentials, or contact a broker.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import queue

import backtrader as bt
from backtrader.position import Position


class SyntheticLiveFeed(bt.feed.DataBase):
    params = (("idle_polls", 20),)

    def __init__(self):
        super().__init__()
        self._polls = 0
        self._polls_since_bar = 0
        self._bars_sent = 0

    def islive(self):
        return True

    def _load(self):
        if self._bars_sent > 0:
            self._polls_since_bar += 1
            self._polls += 1
            if self._polls_since_bar <= self.p.idle_polls:
                return None
            self._polls_since_bar = 0
        if self._bars_sent >= 3:
            return False

        bar_day = 2 + self._bars_sent
        self._bars_sent += 1
        self.lines.datetime[0] = bt.date2num(dt.datetime(2024, 1, bar_day, 9, 30))
        self.lines.open[0] = 10.0
        self.lines.high[0] = 10.0
        self.lines.low[0] = 10.0
        self.lines.close[0] = 10.0
        self.lines.volume[0] = 100.0
        self.lines.openinterest[0] = 0.0
        return True


class FakeCallbackTransport:
    """In-memory stand-in for asynchronous SDK callbacks and cancel control."""

    def __init__(self):
        self.callbacks = queue.SimpleQueue()
        self.submitted_refs = []
        self.cancel_refs = []

    def place_order(self, order_ref):
        self.submitted_refs.append(order_ref)
        self.callbacks.put((order_ref, "Accepted"))

    def cancel_order(self, order_ref):
        self.cancel_refs.append(order_ref)
        self.callbacks.put((order_ref, "Canceled"))

    def fill_order(self, order_ref):
        self.callbacks.put((order_ref, "Completed"))

    def drain(self):
        events = []
        while True:
            try:
                events.append(self.callbacks.get_nowait())
            except queue.Empty:
                return events


class ProbeBroker(bt.BrokerBase):
    """Minimal BrokerBase adapter mapping fake callbacks to native BT orders."""

    def __init__(self):
        super().__init__()
        self.transport = FakeCallbackTransport()
        self.notifs = collections.deque()
        self.orders = {}
        self.positions = collections.defaultdict(Position)
        self.cash = 1000.0
        self.startingcash = self.cash
        self.pump_calls = 0
        self.fill_queued = False
        self.fill_due_pump = None

    def start(self):
        super().start()

    def getcash(self):
        return self.cash

    def getvalue(self, datas=None):
        return self.cash

    def getposition(self, data):
        return self.positions[data]

    def get_notification(self):
        return self.notifs.popleft() if self.notifs else None

    def notify(self, order):
        self.notifs.append(order.clone())

    def _create_order(self, order_cls, owner, data, size, **kwargs):
        order = order_cls(owner=owner, data=data, size=size, **kwargs)
        order.comminfo = self.getcommissioninfo(data)
        self.orders[order.ref] = order
        order.submit(self)
        self.transport.place_order(order.ref)
        return order

    def buy(self, owner, data, size, price=None, plimit=None, exectype=None,
            valid=None, tradeid=0, oco=None, trailamount=None,
            trailpercent=None, **kwargs):
        return self._create_order(
            bt.BuyOrder, owner, data, size, price=price, pricelimit=plimit,
            exectype=exectype, valid=valid, tradeid=tradeid,
            trailamount=trailamount, trailpercent=trailpercent, **kwargs)

    def sell(self, owner, data, size, price=None, plimit=None, exectype=None,
             valid=None, tradeid=0, oco=None, trailamount=None,
             trailpercent=None, **kwargs):
        return self._create_order(
            bt.SellOrder, owner, data, size, price=price, pricelimit=plimit,
            exectype=exectype, valid=valid, tradeid=tradeid,
            trailamount=trailamount, trailpercent=trailpercent, **kwargs)

    def submit(self, order):
        self.orders[order.ref] = order
        order.submit(self)
        self.transport.place_order(order.ref)
        return order

    def cancel(self, order):
        self.transport.cancel_order(order.ref)
        return True

    def _apply_callback(self, order_ref, status):
        order = self.orders[order_ref]
        if status == "Accepted":
            order.accept(self)
        elif status == "Completed":
            size = order.created.size
            price = 10.0
            order.execute(
                dt=bt.date2num(dt.datetime(2024, 1, 2, 9, 31)),
                size=size, price=price,
                closed=0, closedvalue=0.0, closedcomm=0.0,
                opened=size, openedvalue=abs(size * price), openedcomm=0.0,
                margin=None, pnl=0.0, psize=size, pprice=price)
        elif status == "Canceled":
            order.cancel()
            order.executed.dt = bt.date2num(dt.datetime(2024, 1, 2, 9, 31))
        else:
            raise AssertionError("unexpected fake callback: " + status)
        self.notify(order)

    def next(self):
        self.pump_calls += 1
        if (self.fill_due_pump is not None and
                self.pump_calls >= self.fill_due_pump):
            self.transport.fill_order(self.transport.submitted_refs[0])
            self.fill_due_pump = None
        events = self.transport.drain()
        for order_ref, status in events:
            self._apply_callback(order_ref, status)

        # The synthetic execution follows acceptance on a later pump, like an
        # asynchronous callback arriving after the submit response.
        accepted = any(status == "Accepted" for _, status in events)
        if accepted and not self.fill_queued and self.transport.submitted_refs:
            self.fill_due_pump = self.pump_calls + 17
            self.fill_queued = True


class ProbeStrategy(bt.Strategy):
    def __init__(self):
        self.callbacks = []
        self.next_calls = 0
        self.orders_by_ref = {}

    def next(self):
        if self.orders_by_ref:
            self.next_calls += 1
            return
        first = self.buy(size=1)
        second = self.buy(size=1)
        self.orders_by_ref = {first.ref: first, second.ref: second}
        self.first_ref = first.ref
        self.second_ref = second.ref
        self.next_calls += 1

    def notify_order(self, order):
        self.callbacks.append({
            "ref": order.ref,
            "status": order.getstatusname(),
            "bars_seen": len(self.data),
            "native_order": isinstance(order, bt.Order),
        })
        if order.ref == self.second_ref and order.status == order.Accepted:
            self.cancel(self.orders_by_ref[order.ref])

        statuses = {entry["ref"]: entry["status"] for entry in self.callbacks}
        if (statuses.get(self.first_ref) == "Completed" and
                statuses.get(self.second_ref) == "Canceled"):
            self.env.runstop()

def run_case(quicknotify):
    cerebro = bt.Cerebro(quicknotify=quicknotify, stdstats=False)
    feed = SyntheticLiveFeed(idle_polls=20)
    broker = ProbeBroker()
    cerebro.adddata(feed, name="SYNTHETIC")
    cerebro.setbroker(broker)
    cerebro.addstrategy(ProbeStrategy)
    strategies = cerebro.run()
    strategy = strategies[0]

    result = {
        "quicknotify": quicknotify,
        "idle_polls_between_bars": feed._polls,
        "bars_seen_at_callback": [item["bars_seen"] for item in strategy.callbacks],
        "callbacks": strategy.callbacks,
        "next_calls": strategy.next_calls,
        "broker_pump_calls": broker.pump_calls,
        "cancel_refs": broker.transport.cancel_refs,
        "orders_are_native": all(item["native_order"] for item in strategy.callbacks),
    }
    return result


def main():
    print("Backtrader version:", bt.__version__)
    print("Backtrader module:", bt.__file__)
    results = [run_case(quicknotify=True), run_case(quicknotify=False)]
    print(json.dumps(results, indent=2, sort_keys=True))

    immediate = results[0]
    assert immediate["orders_are_native"]
    assert immediate["bars_seen_at_callback"]
    assert set(immediate["bars_seen_at_callback"]) == {1}
    assert {event["status"] for event in immediate["callbacks"]} >= {
        "Accepted", "Completed", "Canceled"}
    assert len(immediate["cancel_refs"]) == 1
    assert immediate["next_calls"] == 1

    deferred = results[1]
    assert deferred["orders_are_native"]
    assert set(deferred["bars_seen_at_callback"]) == {2, 3}
    assert deferred["next_calls"] >= 2


if __name__ == "__main__":
    main()
