from __future__ import annotations

import backtrader as bt
import pandas as pd
import pytest

from src.bt_runtime.strategies import sma
from src.bt_runtime.report_replay import (
    ReportReplayMismatch,
    replay_backtrader_order_log,
    verify_python_report_replay,
)
from src.bt_runtime.strategies.python_upload import (
    PythonStrategyRunError,
    _synthetic_bar_records,
    run_python_upload_backtest,
)
from src.execution.contracts import StrategySpec, canonical_sha256
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.versions import StrategyVersionStore


def make_spec():
    return StrategySpec.model_validate({
        "schema_version": "1.0",
        "strategy_id": "python_upload_fixture",
        "source_kind": "python_upload",
        "template_id": None,
        "template_version": None,
        "source_artifact_id": "upload-fixture-1",
        "instrument_ids": ["synthetic_equity_demo"],
        "bar_policy": {
            "timeframe": "1d", "session": "regular", "completed_only": True,
            "calendar_id": "synthetic_test_calendar", "timezone": "UTC",
        },
        "parameters": {},
        "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
        "execution_policy": {
            "decision": "completed_bar_available", "dispatch": "next_regular_session",
            "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
            "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause",
        },
    })


def bars():
    index = pd.date_range("2024-01-01", periods=7, freq="B", tz="UTC", name="date")
    opens = [10.0, 10.25, 10.5, 10.75, 11.0, 11.25, 11.5]
    closes = [10.1, 10.4, 10.6, 10.9, 11.2, 11.4, 11.6]
    return pd.DataFrame({
        "open": opens,
        "high": [value + 1 for value in opens],
        "low": [value - 1 for value in opens],
        "close": closes,
        "volume": [1000] * len(index),
    }, index=index)


class RecordingSandbox:
    def __init__(self, propose_sell: bool = False, sell_qty: int = 3, reenter: bool = False):
        self.requests = []
        self.propose_sell = propose_sell
        self.sell_qty = sell_qty
        self.reenter = reenter

    def run_json(self, request):
        self.requests.append(request)
        bar = request["event"]["bar"]
        day = bar["session_date"]
        proposal = []
        if day == "2024-01-04":
            proposal = [{"side": "BUY", "integer_qty": 2, "reason": "fixture entry"}]
        if self.propose_sell and day == "2024-01-05":
            proposal = [{"side": "SELL", "integer_qty": self.sell_qty, "reason": "fixture exit"}]
        if self.reenter and day == "2024-01-08":
            proposal = [{"side": "BUY", "integer_qty": 1, "reason": "fixture re-entry"}]
        return type("Result", (), {"response": {
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"last_day": day},
            "order_proposals": proposal,
        }})()


def prepare_version(tmp_path):
    spec = make_spec()
    store = StrategyVersionStore(tmp_path / "versions")
    source = b"def on_event(event, state):\n    return {\"state\": state, \"order_proposals\": []}\n"
    record = store.create_version(
        spec,
        engine_version=f"backtrader/{bt.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
        upload_source=source,
    )
    return spec, store, record


def test_python_upload_uses_native_backbroker_orders_and_next_bar_fill(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)
    sandbox = RecordingSandbox()

    report = run_python_upload_backtest(
        spec, fixture, manifest,
        version_id=version.version_id,
        versions=versions,
        sandbox=sandbox,
        initial_cash=1000,
        commission_rate=0.001,
        slippage_fraction=0.01,
    )

    assert report.source_kind == "python_upload"
    assert report.strategy_version_hash == version.version_id
    assert report.engine == f"Backtrader {bt.__version__} / BackBroker"
    assert len(sandbox.requests) == len(fixture)
    assert all(set(request) == {
        "op", "protocol_version", "request_id", "source", "event_id", "event", "state"
    } for request in sandbox.requests)
    assert all(set(request["event"]) == {"bar"} for request in sandbox.requests)
    assert [order["side"] for order in report.orders] == ["BUY"]
    order = report.orders[0]
    assert order["requested_qty"] == 2  # Explicit shares pass through unchanged.
    assert order["status"] == "Completed"
    assert order["filled_qty"] == 2
    assert order["status_history"][-1] == "Completed"
    assert len(report.fills) == 1
    assert report.fills[0]["qty"] == 2
    assert report.fills[0]["price"] == pytest.approx(
        fixture.loc[pd.Timestamp("2024-01-05", tz="UTC"), "open"] * 1.01
    )
    assert report.fills[0]["executed_at"] == "2024-01-05"
    assert report.total_fees == pytest.approx(report.fills[0]["commission"])
    assert report.final_position["qty"] == 2
    assert len(report.equity_curve) == len(fixture)
    assert report.equity_curve[-1]["equity"] == pytest.approx(report.final_value)
    assert report.decision_log == ({
        "seq": 4,
        "session_date": "2024-01-04",
        "side": "BUY",
        "requested_qty": 2,
        "reason": "fixture entry",
    },)
    assert report.execution_integrity["status"] == "verified"
    assert report.execution_integrity["engine"] == "second Backtrader Cerebro / BackBroker in separate local process"
    assert report.provenance["cost"]["slippage_fraction"] == "0.01"
    assert report.execution_integrity["slippage_fraction"] == 0.01


def test_explicit_buy_over_cash_is_not_resized_and_backbroker_margin_replays(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)

    class OverCashSandbox(RecordingSandbox):
        def run_json(self, request):
            self.requests.append(request)
            day = request["event"]["bar"]["session_date"]
            proposals = (
                [{"side": "BUY", "integer_qty": 200, "reason": "explicit over-cash request"}]
                if day == "2024-01-04" else []
            )
            return type("Result", (), {"response": {
                "protocol_version": request["protocol_version"],
                "request_id": request["request_id"],
                "event_id": request["event_id"],
                "state": {"last_day": day},
                "order_proposals": proposals,
            }})()

    # The decision is made at a $10.90 close and would execute at the next
    # bar's $11.00 open. Two hundred shares cost $2,200 before commission,
    # against only $1,000 of BackBroker cash.
    assert fixture.loc["2024-01-04", "close"] == 10.9
    assert fixture.loc["2024-01-05", "open"] == 11.0
    sandbox = OverCashSandbox()
    report = run_python_upload_backtest(
        spec, fixture, manifest, version_id=version.version_id,
        versions=versions, sandbox=sandbox, initial_cash=1000,
    )

    assert len(sandbox.requests) == len(fixture)
    assert report.decision_log == ({
        "seq": 4,
        "session_date": "2024-01-04",
        "side": "BUY",
        "requested_qty": 200,
        "reason": "explicit over-cash request",
    },)
    assert len(report.orders) == 1
    order = report.orders[0]
    assert order["requested_qty"] == 200
    assert order["status"] == "Margin"
    assert order["status_history"] == ["Submitted", "Margin"]
    assert order["filled_qty"] == 0
    assert order["remaining_qty"] == 200
    assert report.fills == ()
    assert report.total_fees == 0
    assert report.final_cash == 1000
    assert report.final_value == 1000
    assert report.final_position == {"qty": 0, "average_price": 0.0}
    assert report.execution_integrity["status"] == "verified"
    assert report.execution_integrity["engine"] == "second Backtrader Cerebro / BackBroker in separate local process"
    assert {"orders", "fills", "final_cash", "final_value", "final_position"}.issubset(
        report.execution_integrity["checked_fields"]
    )


def test_python_upload_ignores_warmup_orders_and_reports_evaluation_scope(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 2)

    class WarmupAndEvaluationSandbox(RecordingSandbox):
        def run_json(self, request):
            self.requests.append(request)
            day = request["event"]["bar"]["session_date"]
            proposals = []
            if day == "2024-01-01":
                proposals = [{"side": "BUY", "integer_qty": 3, "reason": "warmup probe"}]
            elif day == "2024-01-04":
                proposals = [{"side": "BUY", "integer_qty": 2, "reason": "evaluation entry"}]
            return type("Result", (), {"response": {
                "protocol_version": request["protocol_version"],
                "request_id": request["request_id"],
                "event_id": request["event_id"],
                "state": {"last_day": day},
                "order_proposals": proposals,
            }})()

    sandbox = WarmupAndEvaluationSandbox()
    report = run_python_upload_backtest(
        spec, fixture, manifest, version_id=version.version_id,
        versions=versions, sandbox=sandbox, initial_cash=1000,
    )

    assert len(sandbox.requests) == len(fixture)  # Warmup still builds strategy state.
    assert report.warmup_proposal_count == 1
    assert report.provenance["evaluation_window"] == {
        "warmup_start": "2024-01-01",
        "warmup_end": "2024-01-02",
        "evaluation_start": "2024-01-03",
        "evaluation_end": "2024-01-09",
        "warmup_proposals_ignored": 1,
    }
    assert report.evaluation_bar_count == 5
    assert report.decision_log == ({
        "seq": 4,
        "session_date": "2024-01-04",
        "side": "BUY",
        "requested_qty": 2,
        "reason": "evaluation entry",
    },)
    assert [order["requested_qty"] for order in report.orders] == [2]
    assert [fill["qty"] for fill in report.fills] == [2]
    assert report.final_position["qty"] == 2


def test_python_upload_rejects_invalid_or_uncovered_evaluation_window(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 2)
    invalid = manifest.model_copy(update={"evaluation_start": manifest.warmup_end})

    with pytest.raises(PythonStrategyRunError, match="ordered covered window"):
        run_python_upload_backtest(
            spec, fixture, invalid, version_id=version.version_id,
            versions=versions, sandbox=RecordingSandbox(), initial_cash=1000,
        )

    uncovered = manifest.model_copy(update={
        "evaluation_start": pd.Timestamp("2024-01-06").date(),
    })
    with pytest.raises(PythonStrategyRunError, match="evaluation start must be present"):
        run_python_upload_backtest(
            spec, fixture, uncovered, version_id=version.version_id,
            versions=versions, sandbox=RecordingSandbox(), initial_cash=1000,
        )


def test_independent_replay_rejects_tampered_report_and_decision_log(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)
    report = run_python_upload_backtest(
        spec, fixture, manifest, version_id=version.version_id,
        versions=versions, sandbox=RecordingSandbox(), initial_cash=1000,
    )
    records = _synthetic_bar_records(spec, fixture)
    replay = replay_backtrader_order_log(
        fixture, records, report.decision_log, initial_cash=1000, commission_rate=0.001,
        data_manifest_hash=canonical_sha256(manifest), instrument_id=spec.instrument_ids[0],
        evaluation_start=manifest.evaluation_start,
    )

    tampered_report = report.to_dict()
    tampered_report["final_value"] += 1
    with pytest.raises(ReportReplayMismatch, match="final_value"):
        verify_python_report_replay(
            tampered_report, replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0],
        )

    forged_order = report.to_dict()
    forged_order["orders"][0]["broker_reported_realized_pnl"] = 1_000_000.0
    with pytest.raises(ReportReplayMismatch, match="outside the verified execution schema"):
        verify_python_report_replay(
            forged_order, replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0], strict_order_shape=True,
        )

    tampered_decision = dict(report.decision_log[0], requested_qty=3)
    altered_replay = replay_backtrader_order_log(
        fixture, records, (tampered_decision,), initial_cash=1000, commission_rate=0.001,
        data_manifest_hash=canonical_sha256(manifest), instrument_id=spec.instrument_ids[0],
        evaluation_start=manifest.evaluation_start,
    )
    with pytest.raises(ReportReplayMismatch, match="orders"):
        verify_python_report_replay(
            report.to_dict(), altered_replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0],
        )

    for field, value, message in (
        ("initial_cash", 2000, "initial cash"),
        ("strategy_version_hash", "forged-version", "strategy version"),
        ("engine", "forged-engine", "engine identity"),
    ):
        changed_report = report.to_dict()
        changed_report[field] = value
        with pytest.raises(ReportReplayMismatch, match=message):
            verify_python_report_replay(
                changed_report, replay, initial_cash=1000, commission_rate=0.001,
                data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
                engine=f"Backtrader {bt.__version__} / BackBroker",
                instrument_id=spec.instrument_ids[0],
            )

    changed_cost_report = report.to_dict()
    changed_cost_report["provenance"]["cost"]["commission_rate"] = "0"
    with pytest.raises(ReportReplayMismatch, match="commission declaration"):
        verify_python_report_replay(
            changed_cost_report, replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0],
        )

    changed_manifest_report = report.to_dict()
    changed_manifest_report["data_manifest"]["provider"] = "forged-source"
    with pytest.raises(ReportReplayMismatch, match="manifest content"):
        verify_python_report_replay(
            changed_manifest_report, replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0],
        )

    changed_decision_report = report.to_dict()
    changed_decision_report["decision_log"][0]["requested_qty"] = 4
    with pytest.raises(ReportReplayMismatch, match="decision log changed"):
        verify_python_report_replay(
            changed_decision_report, replay, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), strategy_version_hash=version.version_hash,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=spec.instrument_ids[0],
        )

    changed_bars = fixture.copy()
    changed_bars.iloc[0, changed_bars.columns.get_loc("open")] += 1
    with pytest.raises(ReportReplayMismatch, match="Bar open"):
        replay_backtrader_order_log(
            changed_bars, records, report.decision_log, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), instrument_id=spec.instrument_ids[0],
        )

    unordered_decisions = (
        {"seq": 5, "session_date": records[4].session_date.isoformat(), "side": "SELL", "requested_qty": 1, "reason": "later"},
        report.decision_log[0],
    )
    with pytest.raises(ReportReplayMismatch, match="strictly increasing"):
        replay_backtrader_order_log(
            fixture, records, unordered_decisions, initial_cash=1000, commission_rate=0.001,
            data_manifest_hash=canonical_sha256(manifest), instrument_id=spec.instrument_ids[0],
        )


def test_independent_replay_verifies_closed_trade_and_still_open_position(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)
    report = run_python_upload_backtest(
        spec, fixture, manifest, version_id=version.version_id,
        versions=versions, sandbox=RecordingSandbox(propose_sell=True, sell_qty=2, reenter=True),
        initial_cash=1000, commission_rate=0.001,
    )
    assert report.execution_integrity["status"] == "verified"
    assert len(report.closed_trades) == 1
    assert report.final_position["qty"] == 1


def test_python_upload_rejects_sell_larger_than_long_position_before_submission(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars()
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)

    with pytest.raises(PythonStrategyRunError, match="sell proposal exceeds"):
        run_python_upload_backtest(
            spec, fixture, manifest,
            version_id=version.version_id,
            versions=versions,
            sandbox=RecordingSandbox(propose_sell=True),
        )


def test_future_bar_perturbation_cannot_change_prior_python_decisions_or_fills(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    original = bars()
    perturbed = original.copy()
    perturbed.loc[perturbed.index[-1], ["open", "high", "low", "close"]] = [30, 32, 29, 31]
    original_manifest = sma._build_synthetic_manifest(spec, original, 1)
    perturbed_manifest = sma._build_synthetic_manifest(spec, perturbed, 1)

    first = run_python_upload_backtest(
        spec, original, original_manifest, version_id=version.version_id,
        versions=versions, sandbox=RecordingSandbox(), initial_cash=1000,
    )
    second = run_python_upload_backtest(
        spec, perturbed, perturbed_manifest, version_id=version.version_id,
        versions=versions, sandbox=RecordingSandbox(), initial_cash=1000,
    )

    prior_order_fields = ("side", "requested_qty", "decision_date", "status", "filled_qty")
    assert [tuple(order[key] for key in prior_order_fields) for order in first.orders] == [
        tuple(order[key] for key in prior_order_fields) for order in second.orders
    ]
    assert first.fills[0] == second.fills[0]
    assert first.final_value != second.final_value


def test_runner_rejects_more_than_one_thousand_events_before_starting_workers(tmp_path):
    spec, versions, version = prepare_version(tmp_path)
    fixture = bars().iloc[:1].copy()
    fixture = pd.concat([fixture] * 1_001, ignore_index=True)
    fixture.index = pd.date_range("2020-01-01", periods=len(fixture), freq="B", tz="UTC", name="date")
    manifest = sma._build_synthetic_manifest(spec, fixture, 1)
    sandbox = RecordingSandbox()

    with pytest.raises(PythonStrategyRunError, match="limited to 1000"):
        run_python_upload_backtest(
            spec, fixture, manifest, version_id=version.version_id,
            versions=versions, sandbox=sandbox,
        )
    assert sandbox.requests == []
