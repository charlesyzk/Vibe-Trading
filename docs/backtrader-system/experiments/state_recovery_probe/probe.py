#!/usr/bin/env python3
"""Offline Backtrader state recovery probe; no broker connection is used."""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from typing import Any

import backtrader as bt
import pandas as pd


SCHEMA_VERSION = 1
CUTOFF = 24
INITIAL_CASH = 100_000.0


class RecursiveVolatility(bt.Indicator):
    """Recursive EW mean/variance: state depends on the entire input prefix."""

    lines = ("mean", "sigma")
    params = (("alpha", 0.17),)

    def __init__(self):
        self.addminperiod(2)
        self._mean = None
        self._variance = 0.0

    def next(self):
        value = float(self.data[0])
        if self._mean is None:
            self._mean = value
        else:
            delta = value - self._mean
            self._mean += self.p.alpha * delta
            self._variance = (1.0 - self.p.alpha) * (
                self._variance + self.p.alpha * delta * delta
            )
        self.lines.mean[0] = self._mean
        self.lines.sigma[0] = self._variance**0.5


class StatefulStrategy(bt.Strategy):
    params = (("restore_json", None), ("warmup_through", 0))

    def __init__(self):
        self.ema = bt.ind.EMA(self.data.close, period=7)
        self.volatility = RecursiveVolatility(self.data.close, alpha=0.17)
        self.processed_events = 0
        self.trend_events = 0
        self.cooldown = 0
        self.signal_count = 0
        self.last_action = "none"
        self.last_order_status = "none"
        self.order_notification_count = 0
        self.order_fill_count = 0
        self.events: list[dict[str, Any]] = []

    def start(self):
        if self.p.restore_json is not None:
            self.restore_state(self.p.restore_json)

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "state_schema_version": SCHEMA_VERSION,
            "processed_events": self.processed_events,
            "trend_events": self.trend_events,
            "cooldown": self.cooldown,
            "signal_count": self.signal_count,
            "last_action": self.last_action,
            "last_order_status": self.last_order_status,
            "order_notification_count": self.order_notification_count,
            "order_fill_count": self.order_fill_count,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        if state.get("state_schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported state_schema_version")
        for name in (
            "processed_events", "trend_events", "cooldown", "signal_count",
            "last_action", "last_order_status", "order_notification_count",
            "order_fill_count",
        ):
            if name not in state:
                raise ValueError(f"missing state field: {name}")
            setattr(self, name, state[name])

    def notify_order(self, order):
        self.order_notification_count += 1
        self.last_order_status = order.getstatusname()
        if order.status == order.Completed:
            self.order_fill_count += 1

    def next(self):
        bar_index = len(self.data)
        if bar_index <= self.p.warmup_through:
            return

        close = float(self.data.close[0])
        ema = float(self.ema[0])
        sigma = float(self.volatility.sigma[0])
        self.processed_events += 1
        trending = close > ema and sigma > 0.55
        if trending:
            self.trend_events += 1
            self.cooldown += 1
        else:
            self.cooldown = max(0, self.cooldown - 2)

        decision = "HOLD"
        if trending and self.cooldown >= 3 and self.trend_events % 4 == 0:
            decision = "BUY"
            self.signal_count += 1
            self.cooldown = 0
            self.last_action = "BUY"
            # Fixed-size local BackBroker orders exercise real BT notifications.
            # The strategy does not use position/cash to choose its decision.
            self.buy(size=2)
        elif not trending:
            self.last_action = "HOLD"

        self.events.append(
            {
                "bar_index": bar_index,
                "date": self.data.datetime.date(0).isoformat(),
                "close": round(close, 8),
                "ema": round(ema, 8),
                "recursive_sigma": round(sigma, 8),
                "decision": decision,
                "state": self.snapshot_state(),
            }
        )


def make_frame(count: int) -> pd.DataFrame:
    start = datetime(2025, 1, 2)
    rows = []
    for i in range(count):
        # Deterministic trend + cyclic shocks exercises recursive indicator state.
        close = 50.0 + 0.19 * i + 3.1 * math.sin(i * 0.67)
        rows.append((start + timedelta(days=i), close))
    return pd.DataFrame(
        {"open": [x[1] for x in rows], "high": [x[1] + 0.4 for x in rows],
         "low": [x[1] - 0.4 for x in rows], "close": [x[1] for x in rows],
         "volume": [10_000] * count},
        index=pd.DatetimeIndex([x[0] for x in rows]),
    )


def run(frame: pd.DataFrame, restore_json=None, warmup_through=0):
    cerebro = bt.Cerebro(preload=False, runonce=False, stdstats=False)
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(
        StatefulStrategy, restore_json=restore_json,
        warmup_through=warmup_through,
    )
    result = cerebro.run()
    return result[0], cerebro.broker.getvalue(), cerebro.broker.getcash()


def main() -> int:
    frame = make_frame(CUTOFF + 20)
    prefix_strategy, _, _ = run(frame.iloc[:CUTOFF].copy())
    checkpoint = prefix_strategy.snapshot_state()
    checkpoint_json = json.loads(json.dumps(checkpoint, sort_keys=True))

    continuous, continuous_value, continuous_cash = run(frame)
    resumed, resumed_value, resumed_cash = run(
        frame, restore_json=checkpoint_json, warmup_through=CUTOFF
    )
    continuous_future = [event for event in continuous.events if event["bar_index"] > CUTOFF]
    resumed_future = [event for event in resumed.events if event["bar_index"] > CUTOFF]
    if not continuous_future or len(continuous_future) != len(resumed_future):
        raise AssertionError("future event counts differ")
    if continuous_future != resumed_future:
        for expected, actual in zip(continuous_future, resumed_future):
            if expected != actual:
                raise AssertionError(
                    "first mismatch:\n"
                    + json.dumps({"continuous": expected, "resumed": actual}, indent=2)
                )
        raise AssertionError("future event streams differ")

    print(f"backtrader_version={bt.__version__}")
    print(f"python_version={sys.version.split()[0]}")
    print(f"input_bars={len(frame)} cutoff_bar_index={CUTOFF}")
    print(f"checkpoint_json={json.dumps(checkpoint_json, sort_keys=True, separators=(',', ':'))}")
    print(f"compared_future_events={len(continuous_future)}")
    print(f"future_decisions={json.dumps([e['decision'] for e in continuous_future])}")
    print(
        "future_buy_bar_indices="
        f"{json.dumps([e['bar_index'] for e in continuous_future if e['decision'] == 'BUY'])}"
    )
    print(
        "future_order_notifications="
        f"continuous:{continuous.snapshot_state()['order_notification_count'] - checkpoint['order_notification_count']} "
        f"resumed:{resumed.snapshot_state()['order_notification_count'] - checkpoint['order_notification_count']}"
    )
    print(f"all_future_event_state_and_decision_equal=true")
    print(
        "broker_end_state="
        f"continuous(value={continuous_value:.2f},cash={continuous_cash:.2f}) "
        f"resumed(value={resumed_value:.2f},cash={resumed_cash:.2f})"
    )
    print("probe_result=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
