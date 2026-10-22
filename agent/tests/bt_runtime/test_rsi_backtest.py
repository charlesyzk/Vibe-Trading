from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from src.bt_runtime.strategies.sma import compile_strategy, run_synthetic_backtest, wilder_rsi


def make_spec(*, period=2, entry_below=40, exit_above=60, allocation="0.50", parameters=None):
    return {
        "schema_version": "1.0", "strategy_id": "rsi-test",
        "source_kind": "natural_language_template", "template_id": "rsi_threshold",
        "template_version": "1.0", "instrument_ids": ["synthetic_equity_demo"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "synthetic_test_calendar", "timezone": "UTC"},
        "parameters": parameters or {"period": period, "entry_below": entry_below, "exit_above": exit_above},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                            "allocation_fraction": allocation, "rebalance": "on_signal_transition",
                            "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
    }


def bars_from_closes(closes):
    index = pd.date_range("2024-01-01", periods=len(closes), freq="B", tz="UTC", name="date")
    return pd.DataFrame({"open": closes, "high": [v + 1 for v in closes],
                         "low": [v - 1 for v in closes], "close": closes,
                         "volume": [100_000] * len(closes)}, index=index)


def test_wilder_rsi_uses_sma_seed_and_wilder_recurrence():
    values = wilder_rsi([10, 11, 10, 12, 11], 2)
    assert values[:2] == [None, None]
    assert values[2] == pytest.approx(50)
    assert values[3] == pytest.approx(83.33333333333333)
    assert values[4] == pytest.approx(50)


def test_wilder_rsi_defines_flat_and_one_sided_series():
    assert wilder_rsi([10, 10, 10, 10], 2)[2:] == [50.0, 50.0]
    assert wilder_rsi([10, 11, 12, 13], 2)[2:] == [100.0, 100.0]
    assert wilder_rsi([13, 12, 11, 10], 2)[2:] == [0.0, 0.0]


@pytest.mark.parametrize("parameters", [
    {"period": True, "entry_below": 30, "exit_above": 70},
    {"period": 1, "entry_below": 30, "exit_above": 70},
    {"period": 14, "entry_below": 70, "exit_above": 30},
    {"period": 14, "entry_below": 30, "exit_above": 101},
    {"period": 14, "entry_below": 30, "exit_above": 70, "other": 1},
])
def test_rsi_compiler_rejects_invalid_or_extra_parameters(parameters):
    with pytest.raises(ValueError):
        compile_strategy(make_spec(parameters=parameters))


def test_real_cerebro_rsi_crosses_down_then_up_and_fills_next_bar():
    closes = [10, 11, 10, 9, 10, 11, 10]
    report = run_synthetic_backtest(
        make_spec(), bars_from_closes(closes), strategy_budget=Decimal("40000"),
        fee_reserve=Decimal("25"), price_buffer_fraction=Decimal("0.01"),
    )
    assert report.template_id == "rsi_threshold"
    assert report.parameters == {"period": 2, "entry_below": 40.0, "exit_above": 60.0}
    assert [row["side"] for row in report.orders] == ["BUY", "SELL"]
    assert [row["decision_date"] for row in report.orders] == ["2024-01-04", "2024-01-05"]
    assert [row["executed_at"] for row in report.fills] == ["2024-01-05", "2024-01-08"]
    assert report.fills[0]["price"] == pytest.approx(closes[4])
    assert report.fills[1]["price"] == pytest.approx(closes[5])
    assert report.final_position["qty"] == 0


def test_rsi_does_not_chase_when_first_valid_rsi_is_already_below_entry():
    closes = [10, 9, 8, 7, 8, 9]
    report = run_synthetic_backtest(
        make_spec(), bars_from_closes(closes), strategy_budget=Decimal("40000"),
        fee_reserve=Decimal("25"), price_buffer_fraction=Decimal("0.01"),
    )
    assert report.orders == ()


def test_rsi_equality_does_not_trigger_until_a_strict_downward_cross():
    closes = [10, 11, 10, 9.75, 9, 10]
    report = run_synthetic_backtest(
        make_spec(), bars_from_closes(closes), strategy_budget=Decimal("40000"),
        fee_reserve=Decimal("25"), price_buffer_fraction=Decimal("0.01"),
    )
    buy_orders = [row for row in report.orders if row["side"] == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0]["decision_date"] == "2024-01-05"
