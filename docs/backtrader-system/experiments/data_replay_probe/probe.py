"""Offline Backtrader probes for released-bar visibility and report replay.

Run from the Vibe-Trading repository root with the Backtrader source checkout
on PYTHONPATH, as documented in this directory's README.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import backtrader as bt
import pandas as pd


@dataclass(frozen=True)
class Row:
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class ReleasedRows(bt.feed.DataBase):
    """Feed holds only an iterator and appends one record when Cerebro asks."""

    lines = ("open", "high", "low", "close", "volume", "openinterest")
    params = (("rows", None),)

    def start(self):
        super().start()
        self._rows_iterator = iter(self.p.rows)

    def _load(self):
        try:
            row = next(self._rows_iterator)
        except StopIteration:
            return False
        self.lines.datetime[0] = bt.date2num(row.dt)
        self.lines.open[0] = row.open
        self.lines.high[0] = row.high
        self.lines.low[0] = row.low
        self.lines.close[0] = row.close
        self.lines.volume[0] = row.volume
        self.lines.openinterest[0] = 0
        return True


def rows() -> list[Row]:
    start = datetime(2026, 1, 5)
    values = [10, 11, 12, 9, 13, 14]
    return [
        Row(start + timedelta(days=i), value, value + 1, value - 1, value, 1000)
        for i, value in enumerate(values)
    ]


class VisibilityProbe(bt.Strategy):
    def __init__(self):
        self.observed = []

    def next(self):
        count = len(self.data.close.array)
        source_rows = getattr(self.data.p, "rows", None)
        if source_rows is not None and len(source_rows) > len(self):
            future_source_close = source_rows[len(self)].close
        else:
            frame = getattr(self.data.p, "dataname", None)
            future_source_close = (
                float(frame.iloc[len(self)]["close"])
                if frame is not None and len(frame) > len(self)
                else None
            )
        future_value = None
        future_error = None
        try:
            future_value = self.data.close[1]
        except Exception as exc:  # capture engine boundary behavior
            future_error = type(exc).__name__
        self.observed.append(
            {
                "bar": len(self),
                "close": float(self.data.close[0]),
                "line_buffer_length": count,
                "positive_index_value": future_value,
                "positive_index_error": future_error,
                "future_close_read_from_feed_params": future_source_close,
            }
        )


class OrdersByEvent(bt.Strategy):
    params = (("events", ()),)

    def __init__(self):
        self.order_log = []

    def next(self):
        bar = len(self)
        if bar in self.p.events:
            side, qty = self.p.events[bar]
            order = self.buy(size=qty) if side == "BUY" else self.sell(size=qty)
            self.order_log.append({"bar": bar, "side": side, "qty": qty, "ref": order.ref})

    def notify_order(self, order):
        if order.status == order.Completed:
            self.order_log.append(
                {
                    "kind": "fill",
                    "ref": order.ref,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "qty": abs(int(order.executed.size)),
                    "price": float(order.executed.price),
                    "commission": float(order.executed.comm),
                    "dt": order.executed.dt,
                }
            )


class ReplayOrders(bt.Strategy):
    """Independent strategy that only reads the captured order decision log."""

    params = (("order_log", ()),)

    def __init__(self):
        self.order_log = []
        self._commands = {item["bar"]: item for item in self.p.order_log}

    def next(self):
        command = self._commands.get(len(self))
        if command is None:
            return
        side, qty = command["side"], command["qty"]
        order = self.buy(size=qty) if side == "BUY" else self.sell(size=qty)
        self.order_log.append({"bar": len(self), "side": side, "qty": qty, "ref": order.ref})

    def notify_order(self, order):
        if order.status == order.Completed:
            self.order_log.append(
                {
                    "kind": "fill",
                    "ref": order.ref,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "qty": abs(int(order.executed.size)),
                    "price": float(order.executed.price),
                    "commission": float(order.executed.comm),
                    "dt": order.executed.dt,
                }
            )


def run_strategy(data, events):
    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)
    cerebro.adddata(data)
    cerebro.addstrategy(OrdersByEvent, events=events)
    cerebro.broker.setcash(1000.0)
    cerebro.broker.setcommission(commission=0.001)
    strategy = cerebro.run()[0]
    return strategy.order_log, float(cerebro.broker.getcash()), float(cerebro.broker.getvalue())


def run_replay(data, order_log):
    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)
    cerebro.adddata(data)
    cerebro.addstrategy(ReplayOrders, order_log=order_log)
    cerebro.broker.setcash(1000.0)
    cerebro.broker.setcommission(commission=0.001)
    strategy = cerebro.run()[0]
    return strategy.order_log, float(cerebro.broker.getcash()), float(cerebro.broker.getvalue())


def stable_order_log(order_log):
    """Ignore Backtrader's process-local order references, retain business facts."""
    return [{key: value for key, value in item.items() if key != "ref"} for item in order_log]


def main():
    source_rows = rows()
    visibility = bt.Cerebro(stdstats=False, preload=False, runonce=False)
    visibility.adddata(ReleasedRows(rows=source_rows))
    visibility.addstrategy(VisibilityProbe)
    observed = visibility.run()[0].observed
    assert len(observed) == len(source_rows)
    assert [item["line_buffer_length"] for item in observed] == list(range(1, len(source_rows) + 1))
    assert all(item["positive_index_value"] is None for item in observed), observed
    assert observed[0]["future_close_read_from_feed_params"] == source_rows[1].close

    events = {1: ("BUY", 2), 3: ("SELL", 1)}
    first = run_strategy(ReleasedRows(rows=source_rows), events)
    replay_orders, replay_cash, replay_value = run_replay(
        ReleasedRows(rows=source_rows),
        [item for item in first[0] if item.get("kind") is None],
    )
    first_orders, first_cash, first_value = first
    assert stable_order_log(first_orders) == stable_order_log(replay_orders)
    assert first_cash == replay_cash
    assert first_value == replay_value
    fills = [item for item in first_orders if item.get("kind") == "fill"]
    assert [(f["side"], f["qty"], f["price"]) for f in fills] == [
        ("BUY", 2, 11.0),
        ("SELL", 1, 9.0),
    ], fills

    # Contrast with PandasData: its DataFrame remains directly reachable from
    # the feed object even when engine preloading is disabled.
    df = pd.DataFrame(
        {
            "open": [row.open for row in source_rows],
            "high": [row.high for row in source_rows],
            "low": [row.low for row in source_rows],
            "close": [row.close for row in source_rows],
            "volume": [row.volume for row in source_rows],
            "openinterest": [0] * len(source_rows),
        },
        index=[row.dt for row in source_rows],
    )
    pandas_cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False)
    pandas_cerebro.adddata(bt.feeds.PandasData(dataname=df))
    pandas_cerebro.addstrategy(VisibilityProbe)
    pandas_seen = pandas_cerebro.run()[0].observed[0]
    assert len(pandas_cerebro.datas[0].p.dataname) == len(source_rows)

    print(
        json.dumps(
            {
                "backtrader_version": bt.__version__,
                "line_buffer_lengths": [item["line_buffer_length"] for item in observed],
                "positive_index_blocked": all(item["positive_index_error"] == "IndexError" for item in observed),
                "next_close_reachable_from_custom_feed_params": observed[0]["future_close_read_from_feed_params"],
                "pandas_feed_first_bar": {
                    "buffer_length": pandas_seen["line_buffer_length"],
                    "future_rows_reachable_from_dataname": len(pandas_cerebro.datas[0].p.dataname),
                },
                "replay": {
                    "orders_and_fills_identical": stable_order_log(first_orders) == stable_order_log(replay_orders),
                    "cash": first_cash,
                    "equity": first_value,
                    "orders_and_fills": stable_order_log(first_orders),
                },
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
