from __future__ import annotations

import hashlib
from decimal import Decimal

import backtrader as bt
import pandas as pd
import pytest

from src.bt_runtime.strategies.sma import compile_strategy, run_synthetic_backtest
from src.execution.contracts import canonical_sha256


def make_spec(**overrides):
    value = {
        "schema_version": "1.0",
        "strategy_id": "sma_synthetic_fixture",
        "source_kind": "natural_language_template",
        "template_id": "sma_cross",
        "template_version": "1.0",
        "instrument_ids": ["synthetic_equity_demo"],
        "bar_policy": {
            "timeframe": "1d",
            "session": "regular",
            "completed_only": True,
            "calendar_id": "synthetic_test_calendar",
            "timezone": "UTC",
        },
        "parameters": {"fast_period": 2, "slow_period": 3},
        "position_policy": {
            "mode": "target_fraction",
            "long_only": True,
            "whole_shares": True,
            "allocation_fraction": "0.50",
            "rebalance": "on_signal_transition",
            "initial_entry": "wait_for_cross",
        },
        "execution_policy": {
            "decision": "completed_bar_available",
            "dispatch": "next_regular_session",
            "order_type": "market",
            "time_in_force": "DAY",
            "outside_rth": False,
            "gap_action": "reject_and_pause",
            "missed_session_action": "expire_and_pause",
        },
    }
    value.update(overrides)
    return value


def bars_from_closes(closes: list[float]) -> pd.DataFrame:
    """Clearly synthetic regular daily OHLCV fixture with next-open gaps."""
    index = pd.date_range("2024-01-01", periods=len(closes), freq="B", tz="UTC", name="date")
    opens = [close + (0.25 if index_no % 2 else 0.0) for index_no, close in enumerate(closes)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) + 1.0 for open_, close in zip(opens, closes)],
            "low": [min(open_, close) - 1.0 for open_, close in zip(opens, closes)],
            "close": closes,
            "volume": [100_000] * len(closes),
        },
        index=index,
    )


def run_fixture(spec, bars):
    return run_synthetic_backtest(
        spec,
        bars,
        strategy_budget=Decimal("40000"),
        fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"),
    )


def run_with_seeded_position(spec, bars, *, quantity: int, price: float):
    """Run the real strategy/BackBroker with a synthetic pre-existing holding.

    Backtrader's standard BackBroker has no public initial-position constructor.
    The test seeds its native Position from Strategy.start(), before the first
    event, and removes the matching acquisition cost from cash. No orders or
    fills are fabricated; all subsequent strategy orders use BackBroker.
    """
    compiled = compile_strategy(spec)

    class SeededPositionStrategy(compiled.strategy_class):
        def start(self):
            position = self.broker.positions[self.data]
            position.set(quantity, price)
            self.broker.cash -= quantity * price

    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False)
    cerebro.broker.setcash(100_000.0)
    cerebro.broker.setcommission(commission=0.001)
    cerebro.adddata(bt.feeds.PandasData(dataname=bars, timeframe=bt.TimeFrame.Days))
    cerebro.addstrategy(
        SeededPositionStrategy,
        strategy_budget=Decimal("40000"),
        fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"),
    )
    return cerebro.run()[0]


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    # First valid fast/slow relationship is bearish. A bullish cross triggers
    # entry, a bearish cross exits, and a later bullish cross leaves a position
    # open at the end so the report includes both fills and residual holdings.
    return bars_from_closes([10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12])


def test_compiler_accepts_only_trusted_sma_template_and_exact_parameters():
    compiled = compile_strategy(make_spec())
    assert compiled.template_id == "sma_cross"
    assert compiled.template_version == "1.0"
    assert compiled.fast_period == 2
    assert compiled.slow_period == 3
    assert compiled.allocation_fraction == 0.5

    unsupported_source = make_spec(source_kind="python_upload", template_id=None, template_version=None, source_artifact_id="upload-1")
    unsupported_source["position_policy"] = {
        "mode": "explicit_quantity",
        "long_only": True,
        "whole_shares": True,
    }
    with pytest.raises(ValueError, match="natural_language_template"):
        compile_strategy(unsupported_source)

    with pytest.raises(ValueError, match="exactly fast_period and slow_period"):
        compile_strategy(make_spec(parameters={"fast_period": 2, "slow_period": 3, "threshold": 0}))


def test_real_backtrader_run_reports_orders_fills_fees_cash_and_open_position(synthetic_bars):
    report = run_fixture(make_spec(), synthetic_bars)
    value = report.to_dict()

    assert report.provenance == {
        "data_source": "synthetic_fixture",
        "data_authenticity": "not_applicable_synthetic_fixture",
        "data_manifest_hash": report.data_manifest_hash,
        "evaluation_window": {
            "warmup_start": "2024-01-01",
            "warmup_end": "2024-01-03",
            "evaluation_start": "2024-01-04",
            "evaluation_end": "2024-01-16",
        },
        "sizing": {
            "strategy_budget": "40000",
            "fee_reserve": "25",
            "price_buffer_fraction": "0.01",
        },
        "cost": {
            "commission_model": "BackBroker percentage commission",
            "commission_rate": "0.001",
            "fee_reserve": "25",
            "slippage_model": "BackBroker percentage slippage at next-bar market open; capped to bar range",
            "slippage_fraction": "0.0",
        },
    }
    assert "Backtrader 1.9.78.123 / BackBroker" == report.engine
    assert [order["side"] for order in report.orders] == ["BUY", "SELL", "BUY"]
    assert report.orders[0]["target_shares"] == 3960  # floor(min(50,000, 40,000) / (10 * 1.01))
    assert report.orders[0]["requested_qty"] == 3960
    assert all(order["status"] == "Completed" for order in report.orders)
    assert all(order["status_history"] == ["Created", "Submitted", "Accepted", "Completed"] for order in report.orders)
    assert len(report.fills) == 3
    assert all(fill["qty"] > 0 and fill["price"] > 0 for fill in report.fills)
    for order, fill in zip(report.orders, report.fills, strict=True):
        decision_index = synthetic_bars.index.get_loc(pd.Timestamp(order["decision_date"], tz="UTC"))
        next_bar = synthetic_bars.iloc[decision_index + 1]
        next_date = synthetic_bars.index[decision_index + 1].date().isoformat()
        assert fill["order_ref"] == order["order_ref"]
        assert fill["executed_at"] == next_date
        assert fill["price"] == pytest.approx(next_bar["open"])
    assert report.total_fees == pytest.approx(sum(fill["commission"] for fill in report.fills))
    assert report.total_fees > 0
    assert report.initial_cash == 100_000
    assert report.final_cash > 0
    assert report.final_value > 0
    assert report.final_position["qty"] > 0
    assert report.final_position["average_price"] > 0
    assert len(report.equity_curve) == len(synthetic_bars)
    assert report.equity_curve[0]["equity"] == report.initial_cash
    assert report.equity_curve[-1]["equity"] == pytest.approx(report.final_value)
    assert len(report.closed_trades) == 1
    assert report.closed_trades[0]["closed_at"] == report.fills[1]["executed_at"]
    assert report.metrics["closed_trade_count"] == 1
    assert report.metrics["max_drawdown_fraction"] >= 0
    assert report.metrics["max_drawdown_value"] >= 0
    assert value["provenance"]["data_source"] == "synthetic_fixture"
    assert report.data_manifest["synthetic"] is True
    normalized_bars = synthetic_bars.astype(float)
    csv_bytes = normalized_bars.to_csv(
        index=True,
        date_format="%Y-%m-%dT%H:%M:%S%z",
        float_format="%.10f",
        lineterminator="\n",
    ).encode("utf-8")
    assert report.data_manifest["file_sha256"] == hashlib.sha256(csv_bytes).hexdigest()
    assert canonical_sha256(report.data_manifest) == report.data_manifest_hash
    assert len(report.data_manifest_hash) == 64


def test_fee_reserve_reduces_affordable_target_using_buffered_price():
    bars = bars_from_closes([10, 9, 8, 9, 10, 10, 11, 12])
    bars["open"] = bars["close"]
    spec = make_spec(
        position_policy={
            "mode": "target_fraction",
            "long_only": True,
            "whole_shares": True,
            "allocation_fraction": "1.0",
            "rebalance": "on_signal_transition",
            "initial_entry": "wait_for_cross",
        }
    )
    report = run_synthetic_backtest(
        spec,
        bars,
        initial_cash=10_000,
        strategy_budget=Decimal("50000"),
        fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"),
    )

    # Target shares = floor(min(10,000, 50,000) / (10 * 1.01)) = 990.
    # Cash-limited shares = floor((10,000 - 25) / (10 * 1.01)) = 987.
    assert report.orders[0]["target_shares"] == 990
    assert report.orders[0]["sizing_requested_qty"] == 990
    assert report.orders[0]["requested_qty"] == report.orders[0]["accepted_qty"] == 987
    assert report.orders[0]["sizing_reason"] == "cash_shrunk"
    assert report.orders[0]["status"] == "Completed"
    assert report.fills[0]["price"] == 10
    assert report.fills[0]["commission"] == pytest.approx(9.87)


def test_sma_zero_equality_boundaries_only_trade_after_crossing_zero():
    # For fast=2, slow=3, fast-slow difference is proportional to
    # close[t-2] * -2 + close[t-1] + close[t]. This sequence yields:
    # -1 -> 0 (no entry), 0 -> +1 (BUY), +1 -> 0 (no exit), 0 -> -1 (SELL).
    bars = bars_from_closes([20, 20, 18, 22, 16, 28, 2, 40])
    bars["open"] = bars["close"]

    report = run_fixture(make_spec(), bars)

    assert [order["side"] for order in report.orders] == ["BUY", "SELL"]
    assert [order["decision_date"] for order in report.orders] == [
        bars.index[4].date().isoformat(),
        bars.index[6].date().isoformat(),
    ]
    assert [fill["executed_at"] for fill in report.fills] == [
        bars.index[5].date().isoformat(),
        bars.index[7].date().isoformat(),
    ]
    assert report.orders[0]["requested_qty"] == report.fills[0]["qty"]
    assert report.orders[1]["requested_qty"] == report.fills[1]["qty"]
    assert report.orders[1]["requested_qty"] == report.orders[0]["requested_qty"]


def test_initially_bullish_sma_waits_for_new_cross_with_or_without_existing_position():
    # The first valid relationship is bullish; subsequent movement turns
    # bearish, touches zero, then crosses positive again.
    closes = [8, 9, 10, 11, 10, 8, 7, 9, 12, 13]
    bars = bars_from_closes(closes)
    bars["open"] = bars["close"]
    spec = make_spec()

    flat_report = run_fixture(spec, bars)
    assert [order["side"] for order in flat_report.orders] == ["BUY"]
    assert flat_report.orders[0]["decision_date"] == bars.index[8].date().isoformat()
    assert flat_report.fills[0]["executed_at"] == bars.index[9].date().isoformat()

    seeded_qty = 7
    strategy = run_with_seeded_position(spec, bars, quantity=seeded_qty, price=9.0)
    orders = list(strategy.order_rows.values())
    fills = strategy.fill_rows
    assert [order["side"] for order in orders] == ["SELL", "BUY"]
    assert orders[0]["decision_date"] == bars.index[5].date().isoformat()
    assert orders[0]["requested_qty"] == seeded_qty
    assert fills[0]["qty"] == seeded_qty
    assert fills[0]["executed_at"] == bars.index[6].date().isoformat()
    assert orders[1]["decision_date"] == bars.index[8].date().isoformat()
    assert fills[1]["executed_at"] == bars.index[9].date().isoformat()
    assert strategy.position.size == orders[1]["requested_qty"]


def test_two_runs_are_deterministic(synthetic_bars):
    first = run_fixture(make_spec(), synthetic_bars).to_dict()
    second = run_fixture(make_spec(), synthetic_bars.copy()).to_dict()
    assert first == second


def test_future_data_perturbation_does_not_change_prior_decisions_or_fills(synthetic_bars):
    cutoff = pd.Timestamp("2024-01-08", tz="UTC")
    changed_future = synthetic_bars.copy()
    future = changed_future.index > cutoff
    changed_future.loc[future, "close"] = list(range(30, 30 + int(future.sum())))
    changed_future.loc[future, "open"] = changed_future.loc[future, "close"]
    changed_future.loc[future, "high"] = changed_future.loc[future, "close"] + 1
    changed_future.loc[future, "low"] = changed_future.loc[future, "close"] - 1

    original = run_fixture(make_spec(), synthetic_bars).to_dict()
    perturbed = run_fixture(make_spec(), changed_future).to_dict()
    prior_orders = lambda report: [
        {
            key: order[key]
            for key in ("order_ref", "side", "requested_qty", "decision_date")
        }
        for order in report["orders"]
        if order.get("decision_date", "9999") <= cutoff.date().isoformat()
    ]
    prior_fills = lambda report: [
        fill for fill in report["fills"] if fill["executed_at"] <= cutoff.date().isoformat()
    ]
    assert prior_orders(original) == prior_orders(perturbed)
    assert prior_fills(original) == prior_fills(perturbed)
    assert original != perturbed


@pytest.mark.parametrize(
    "change",
    [
        {"parameters": {"fast_period": 3, "slow_period": 3}},
        {"parameters": {"fast_period": True, "slow_period": 3}},
    ],
)
def test_compiler_rejects_invalid_periods(change):
    with pytest.raises(ValueError):
        compile_strategy(make_spec(**change))
