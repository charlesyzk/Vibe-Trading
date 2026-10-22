from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import math

import pandas as pd
import pytest

from src.bt_runtime.report_replay import (
    ReportReplayMismatch,
    replay_backtrader_order_log_subprocess,
    verify_backtest_report_replay,
)
from src.bt_runtime.strategies.sma import run_synthetic_backtest
from src.execution.contracts import Bar


def _hand_calculable_bars() -> tuple[pd.DataFrame, tuple[Bar, ...]]:
    # A BUY crosses up after close 5 and fills on bar 6. A SELL crosses down
    # after close 8 and fills on bar 9. A later BUY crosses up after close 11
    # and remains open at the end. Both adverse fills exercise OHLC caps.
    closes = (10.0, 9.0, 8.0, 9.0, 10.0, 10.05, 11.0, 8.0, 7.0, 6.0, 9.0, 12.0)
    opens = (10.0, 9.0, 8.0, 9.0, 10.0, 10.0, 11.0, 8.0, 6.9, 6.0, 9.0, 12.0)
    highs = (10.2, 9.2, 8.2, 9.2, 10.2, 10.05, 11.2, 8.2, 7.5, 6.2, 9.2, 13.2)
    lows = (9.8, 8.8, 7.8, 8.8, 9.8, 9.8, 10.8, 7.8, 6.3, 5.8, 8.8, 11.8)
    days = pd.date_range("2025-01-02", periods=len(closes), freq="B", tz="UTC")
    bars = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1000.0] * len(closes)},
        index=days + pd.Timedelta(hours=21, minutes=1),
    )
    records = tuple(
        Bar(
            instrument_id="TEST",
            session_date=stamp.date(),
            start_at=datetime.combine(stamp.date(), time(14, 30), tzinfo=timezone.utc),
            end_at=datetime.combine(stamp.date(), time(21, 0), tzinfo=timezone.utc),
            available_at=stamp.to_pydatetime(),
            timezone="UTC",
            open=Decimal(str(op)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal("1000"),
            currency="USD",
            is_final=True,
            source="synthetic_fixture",
            adjustment="raw",
        )
        for stamp, op, high, low, close in zip(days + pd.Timedelta(hours=21, minutes=1), opens, highs, lows, closes, strict=True)
    )
    return bars, records


def _hand_calculated_sma_crosses(closes: tuple[float, ...]) -> list[tuple[int, str]]:
    """Independently derive SMA(2/3) cross bars with Decimal arithmetic."""
    values = [Decimal(str(value)) for value in closes]
    differences: list[Decimal | None] = [None] * len(values)
    for index in range(2, len(values)):
        fast = sum(values[index - 1:index + 1], Decimal("0")) / Decimal("2")
        slow = sum(values[index - 2:index + 1], Decimal("0")) / Decimal("3")
        differences[index] = fast - slow

    crosses: list[tuple[int, str]] = []
    for index in range(1, len(differences)):
        previous, current = differences[index - 1], differences[index]
        if previous is None or current is None:
            continue
        if previous <= 0 < current:
            crosses.append((index, "BUY"))
        elif previous >= 0 > current:
            crosses.append((index, "SELL"))
    return crosses


def test_backbroker_cost_cash_equity_and_open_position_match_hand_calculation():
    bars, records = _hand_calculable_bars()
    manual_crosses = _hand_calculated_sma_crosses(tuple(bars["close"].tolist()))
    assert manual_crosses == [(4, "BUY"), (7, "SELL"), (10, "BUY")]
    spec = {
        "schema_version": "1.0",
        "strategy_id": "hand-calculated-accounting",
        "source_kind": "natural_language_template",
        "template_id": "sma_cross",
        "template_version": "1.0",
        "instrument_ids": ["TEST"],
        "bar_policy": {
            "timeframe": "1d", "session": "regular", "completed_only": True,
            "calendar_id": "synthetic_test_calendar", "timezone": "UTC",
        },
        "parameters": {"fast_period": 2, "slow_period": 3},
        "position_policy": {
            "mode": "target_fraction", "long_only": True, "whole_shares": True,
            "allocation_fraction": "0.50", "rebalance": "on_signal_transition",
            "initial_entry": "wait_for_cross",
        },
        "execution_policy": {
            "decision": "completed_bar_available", "dispatch": "next_regular_session",
            "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
            "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause",
        },
    }
    report = run_synthetic_backtest(
        spec,
        bars,
        initial_cash=1000.0,
        strategy_budget=Decimal("1000"),
        fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"),
        commission_rate=0.01,
        slippage_fraction=0.10,
    )

    # Independent arithmetic. First BUY: floor(min($500, $975) / $10.10)=49;
    # adverse $11 execution is capped at the $10.05 high. SELL: open $6.90
    # with 10% adverse slippage would be $6.21, capped at the $6.30 low.
    # The second BUY is floor(($808.2385 * 50%) / ($9 * 1.01))=44; its $13.20
    # adverse fill is capped at the $13.20 high.
    first_qty, first_fill, first_fee = 49, 10.05, 4.9245
    sale_qty, sale_fill, sale_fee = 49, 6.3, 3.087
    final_qty, final_fill, final_fee = 44, 13.2, 5.808
    cash_after_first_buy = 1000.0 - first_qty * first_fill - first_fee
    cash_after_sale = cash_after_first_buy + sale_qty * sale_fill - sale_fee
    expected_cash = cash_after_sale - final_qty * final_fill - final_fee
    expected_fees = first_fee + sale_fee + final_fee
    expected_final_value = expected_cash + final_qty * 12.0
    expected_closed_gross = sale_qty * (sale_fill - first_fill)
    expected_closed_net = expected_closed_gross - first_fee - sale_fee
    expected_peak_equity = cash_after_first_buy + first_qty * 11.0
    expected_max_drawdown_value = expected_peak_equity - expected_final_value
    expected_max_drawdown_fraction = expected_max_drawdown_value / expected_peak_equity

    assert [row["side"] for row in report.orders] == ["BUY", "SELL", "BUY"]
    index_by_date = {
        record.session_date.isoformat(): index for index, record in enumerate(records)
    }
    assert [
        (index_by_date[row["decision_date"]], row["side"])
        for row in report.orders
    ] == manual_crosses
    assert [row["requested_qty"] for row in report.orders] == [first_qty, sale_qty, final_qty]
    assert [row["decision_date"] for row in report.orders] == [
        records[4].session_date.isoformat(), records[7].session_date.isoformat(), records[10].session_date.isoformat(),
    ]
    assert [row["price"] for row in report.fills] == pytest.approx([first_fill, sale_fill, final_fill])
    assert [row["qty"] for row in report.fills] == [first_qty, sale_qty, final_qty]
    assert [row["commission"] for row in report.fills] == pytest.approx([first_fee, sale_fee, final_fee])
    assert [row["executed_at"] for row in report.fills] == [
        records[5].session_date.isoformat(), records[8].session_date.isoformat(), records[11].session_date.isoformat(),
    ]
    assert report.total_fees == pytest.approx(expected_fees)
    assert report.final_cash == pytest.approx(expected_cash)
    assert report.final_position == {"qty": final_qty, "average_price": pytest.approx(final_fill)}
    assert report.final_value == pytest.approx(expected_final_value)
    assert report.equity_curve[5]["cash"] == pytest.approx(cash_after_first_buy)
    assert report.equity_curve[5]["equity"] == pytest.approx(cash_after_first_buy + first_qty * 10.05)
    assert report.equity_curve[8]["cash"] == pytest.approx(cash_after_sale)
    assert report.equity_curve[8]["equity"] == pytest.approx(cash_after_sale)
    assert report.equity_curve[-1]["equity"] == pytest.approx(expected_final_value)
    assert report.metrics["total_return_fraction"] == pytest.approx(expected_final_value / 1000.0 - 1.0)
    assert report.metrics["max_drawdown_value"] == pytest.approx(expected_max_drawdown_value)
    assert report.metrics["max_drawdown_fraction"] == pytest.approx(expected_max_drawdown_fraction)
    expected_evaluation_dates = {
        row["bar"][:10] for row in report.equity_curve
        if report.data_manifest["evaluation_start"] <= row["bar"][:10] <= report.data_manifest["evaluation_end"]
    }
    assert report.evaluation_bar_count == len(expected_evaluation_dates)
    assert report.provenance["evaluation_window"] == {
        key: report.data_manifest[key]
        for key in ("warmup_start", "warmup_end", "evaluation_start", "evaluation_end")
    }
    expected_annualized_return = math.expm1(
        math.log(expected_final_value / 1000.0) / max(1, report.evaluation_bar_count - 1) * 252.0
    )
    assert report.metrics["backtrader_annualized_return"] == pytest.approx(expected_annualized_return)
    assert len(report.closed_trades) == 1
    assert report.closed_trades[0]["opened_at"] == records[5].session_date.isoformat()
    assert report.closed_trades[0]["closed_at"] == records[8].session_date.isoformat()
    assert report.closed_trades[0]["gross_pnl"] == pytest.approx(expected_closed_gross)
    assert report.closed_trades[0]["net_pnl"] == pytest.approx(expected_closed_net)
    assert report.metrics["closed_trade_count"] == 1
    assert report.metrics["closed_trade_win_count"] == 0

    seq_by_date = {record.session_date.isoformat(): index for index, record in enumerate(records, start=1)}
    decision_log = [
        {
            "seq": seq_by_date[row["decision_date"]],
            "session_date": row["decision_date"],
            "side": row["side"],
            "requested_qty": row["requested_qty"],
            "reason": row["sizing_reason"],
        }
        for row in report.orders
    ]
    replay = replay_backtrader_order_log_subprocess(
        bars,
        records,
        decision_log,
        initial_cash=1000.0,
        commission_rate=0.01,
        slippage_fraction=0.10,
        evaluation_start=date.fromisoformat(report.data_manifest["evaluation_start"]),
        data_manifest_hash=report.data_manifest_hash,
        instrument_id="TEST",
    )
    report_payload = report.to_dict()
    report_payload["decision_log"] = decision_log
    report_payload["strategy_version_hash"] = "a" * 64
    report_payload["provenance"]["strategy_version_hash"] = "a" * 64
    verified = verify_backtest_report_replay(
        report_payload,
        replay,
        initial_cash=1000.0,
        commission_rate=0.01,
        slippage_fraction=0.10,
        data_manifest_hash=report.data_manifest_hash,
        strategy_version_hash="a" * 64,
        engine=report.engine,
        instrument_id="TEST",
    )
    assert verified["status"] == "verified"
    assert replay["evaluation_bar_count"] == report.evaluation_bar_count
    assert "evaluation_window" in verified["checked_fields"]
    assert "evaluation_bar_count" in verified["checked_fields"]
    assert [row["price"] for row in replay["fills"]] == pytest.approx([first_fill, sale_fill, final_fill])
    assert replay["total_fees"] == pytest.approx(expected_fees)
    assert replay["final_cash"] == pytest.approx(expected_cash)
    assert replay["final_value"] == pytest.approx(expected_final_value)
    assert replay["closed_trades"][0]["net_pnl"] == pytest.approx(expected_closed_net)
    assert replay["final_position"] == {"qty": final_qty, "average_price": pytest.approx(final_fill)}

    corrupted_report = {**report_payload, "total_fees": report.total_fees + 1.0}
    with pytest.raises(ReportReplayMismatch, match="total_fees"):
        verify_backtest_report_replay(
            corrupted_report,
            replay,
            initial_cash=1000.0,
            commission_rate=0.01,
            slippage_fraction=0.10,
            data_manifest_hash=report.data_manifest_hash,
            strategy_version_hash="a" * 64,
            engine=report.engine,
            instrument_id="TEST",
        )

    corrupted_window = {
        **report_payload,
        "provenance": {
            **report_payload["provenance"],
            "evaluation_window": {
                **report_payload["provenance"]["evaluation_window"],
                "evaluation_start": "2099-01-01",
            },
        },
    }
    with pytest.raises(ReportReplayMismatch, match="evaluation window"):
        verify_backtest_report_replay(
            corrupted_window,
            replay,
            initial_cash=1000.0,
            commission_rate=0.01,
            slippage_fraction=0.10,
            data_manifest_hash=report.data_manifest_hash,
            strategy_version_hash="a" * 64,
            engine=report.engine,
            instrument_id="TEST",
        )

    corrupted_count = {**report_payload, "evaluation_bar_count": report.evaluation_bar_count + 1}
    with pytest.raises(ReportReplayMismatch, match="evaluation bar count"):
        verify_backtest_report_replay(
            corrupted_count,
            replay,
            initial_cash=1000.0,
            commission_rate=0.01,
            slippage_fraction=0.10,
            data_manifest_hash=report.data_manifest_hash,
            strategy_version_hash="a" * 64,
            engine=report.engine,
            instrument_id="TEST",
        )
