"""Routing and run-report integration for the separate execution backend."""

import json
import hashlib
from types import SimpleNamespace
from datetime import datetime, timezone

import pandas as pd
import pytest

from backtest.runner import BacktestConfigSchema, _VALID_ENGINES, main, run_backtrader_backend
from src.bt_runtime.calendars import build_xnys_schedule
from src.tools.backtest_tool import run_backtest


def _fake_yfinance_result(frame, symbol="AAPL.US"):
    from src.execution.yfinance_daily_provider import YFinanceDailyResult, canonical_loader_frame_sha256

    return YFinanceDailyResult(
        symbol=symbol,
        frame=frame,
        provenance={
            "schema": "host-observed-provider-fetch/1",
            "requested_provider": "yfinance",
            "actual_loader_identity": "yfinance",
            "loader_class": "tests.FakeYFinanceLoader",
            "request_parameters": {
                "symbol": symbol, "start_date": "2025-02-03",
                "end_date_inclusive": "2025-02-19", "interval": "1D", "fields": None,
            },
            "retrieved_at": datetime(2025, 2, 20, tzinfo=timezone.utc).isoformat(),
            "loader_cache": "miss",
            "loader_output_sha256": canonical_loader_frame_sha256(frame),
            "identity_authentication": "UNVERIFIED",
            "point_in_time_availability": "UNVERIFIED",
            "adjustment_semantics": "UNVERIFIED",
            "broker_contract_mapping": "UNVERIFIED",
        },
    )


def _spec() -> dict:
    return {
        "schema_version": "1.0",
        "strategy_id": "route_test_sma",
        "source_kind": "natural_language_template",
        "template_id": "sma_cross",
        "template_version": "1.0",
        "instrument_ids": ["us_equity_demo"],
        "bar_policy": {
            "timeframe": "1d",
            "session": "regular",
            "completed_only": True,
            "calendar_id": "us_equity_rth",
            "timezone": "America/New_York",
        },
        "parameters": {"fast_period": 2, "slow_period": 3},
        "position_policy": {
            "mode": "target_fraction",
            "long_only": True,
            "whole_shares": True,
            "allocation_fraction": "0.20",
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


def _backtrader_config() -> dict:
    from src.bt_runtime.strategies import sma as sma_runtime

    return {
        "codes": ["AAPL.US"],
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "source": "yfinance",
        "interval": "1D",
        "engine": "daily",
        "execution_backend": "backtrader",
        "strategy_spec": _spec(),
        "instrument_id": "us_equity_demo",
        "expected_strategy_source_hash": sma_runtime.TRUSTED_TEMPLATE_SOURCE_SHA256,
        "expected_compiler_version": sma_runtime.TRUSTED_TEMPLATE_COMPILER_VERSION,
    }


def test_legacy_engine_values_keep_their_existing_meaning():
    assert _VALID_ENGINES == {"daily", "options"}
    daily = BacktestConfigSchema(
        codes=["AAPL.US"], start_date="2025-01-01", end_date="2025-02-01"
    )
    options = BacktestConfigSchema(
        codes=["AAPL.US"], start_date="2025-01-01", end_date="2025-02-01", engine="options"
    )
    assert daily.execution_backend == "legacy"
    assert daily.engine == "daily"
    assert options.execution_backend == "legacy"
    assert options.engine == "options"


def test_backtrader_schema_requires_strategy_spec_and_instrument_binding():
    valid = BacktestConfigSchema(**_backtrader_config())
    assert valid.execution_backend == "backtrader"
    assert valid.engine == "daily"

    missing_spec = _backtrader_config()
    missing_spec.pop("strategy_spec")
    with pytest.raises(ValueError, match="requires strategy_spec"):
        BacktestConfigSchema(**missing_spec)

    missing_instrument = _backtrader_config()
    missing_instrument.pop("instrument_id")
    with pytest.raises(ValueError, match="requires an explicit instrument_id"):
        BacktestConfigSchema(**missing_instrument)


def test_backtrader_runner_routes_before_legacy_signal_engine_requirement(tmp_path, monkeypatch):
    config = _backtrader_config()
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    # There is deliberately no code/signal_engine.py in this run directory.
    monkeypatch.setattr("src.tools.path_utils.safe_run_dir", lambda _: tmp_path)
    routed = []
    monkeypatch.setattr("backtest.runner.run_backtrader_backend", lambda value, path: routed.append((value, path)))

    main(tmp_path)

    assert len(routed) == 1
    assert routed[0][0]["execution_backend"] == "backtrader"
    assert routed[0][1] == tmp_path


def test_backtest_tool_accepts_backtrader_run_without_fake_signal_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps(_backtrader_config()), encoding="utf-8")

    class Result:
        success = True
        exit_code = 0
        stdout = ""
        stderr = ""
        artifacts = {}

    monkeypatch.setattr("src.tools.backtest_tool.emit_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.tools.backtest_tool.Runner.execute", lambda *args, **kwargs: Result())
    monkeypatch.setattr("src.tools.backtest_tool.RunStateStore.mark_success", lambda *args: None)
    monkeypatch.setattr("src.tools.backtest_tool.collect_ohlcv_paths", lambda _: {})
    monkeypatch.setattr("src.tools.backtest_tool.try_build_backtest_summary", lambda *args: None)

    result = json.loads(run_backtest(str(run_dir)))
    assert result["status"] == "ok"
    assert not (run_dir / "code" / "signal_engine.py").exists()


def test_trusted_route_runs_real_backtrader_and_persists_versioned_report(tmp_path, monkeypatch):
    import backtrader
    import backtest.runner as runner

    run_dir = tmp_path / "runtime" / "runs" / "run_trusted"
    run_dir.mkdir(parents=True)
    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-19").date())
    dates = pd.DatetimeIndex([
        pd.Timestamp(session.session_date).tz_localize("UTC")
        for session in schedule.sessions
    ])
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    bars = pd.DataFrame(
        {
            "open": [value + 0.25 for value in closes],
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100_000] * len(closes),
        },
        index=dates,
    )
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(bars, symbol))

    config = _backtrader_config()
    config.update(start_date="2025-02-03", end_date="2025-02-19")
    report = run_backtrader_backend(config, run_dir)

    assert report["engine"].startswith("Backtrader ")
    assert report["provenance"]["data_source"] == "provider_import"
    assert report["data_manifest"]["synthetic"] is False
    assert report["mode"] == "provider_import_with_pinned_xnys_schedule_unverified_provider"
    assert report["data_manifest"]["calendar_version"] == "XNYS@4.13.2"
    assert report["data_manifest"]["quality_report"]["session_schedule"] == "exact_match"
    assert report["data_manifest"]["quality_report"]["calendar_schedule_sha256"] == schedule.sha256
    assert report["data_manifest"]["quality_report"]["calendar_verification"] == "schedule_hash_matched"
    assert report["strategy_version_id"] == report["strategy_version_hash"]
    from src.execution.nl2_paper_composition_factory import _validate_frozen_nl2_version
    from src.execution.versions import StrategyVersionStore

    version_store = StrategyVersionStore(tmp_path / "runtime" / "strategy-versions")
    version_record = version_store.get_version(report["strategy_version_id"])
    assert version_record.engine_version == f"backtrader/{backtrader.__version__}"
    assert _validate_frozen_nl2_version(version_store, report["strategy_version_id"]) == version_record
    assert [row["side"] for row in report["orders"]] == ["BUY", "SELL", "BUY"]
    assert len(report["fills"]) == 3
    assert report["execution_integrity"]["status"] == "verified"
    assert report["execution_integrity"]["engine"] == (
        "second Backtrader Cerebro / BackBroker in separate local process"
    )
    assert report["evaluation_bar_count"] == len([
        row for row in report["equity_curve"]
        if report["data_manifest"]["evaluation_start"] <= row["bar"][:10]
        <= report["data_manifest"]["evaluation_end"]
    ])
    assert "evaluation_bar_count" in report["execution_integrity"]["checked_fields"]
    assert [row["session_date"] for row in report["decision_log"]] == [
        row["decision_date"] for row in report["orders"]
    ]
    persisted = json.loads((run_dir / "artifacts" / "execution_report.json").read_text())
    assert persisted == json.loads(json.dumps(report))
    from src.bt_runtime.report_artifact_integrity import verify_report_integrity

    assert verify_report_integrity(run_dir / "artifacts" / "execution_report.json") == (True, None)
    market_data = (run_dir / "artifacts" / "market_data.csv").read_bytes()
    assert hashlib.sha256(market_data).hexdigest() == report["data_manifest"]["file_sha256"]
    assert market_data.decode("utf-8").splitlines()[0] == (
        "session_date,start_at,end_at,available_at,open,high,low,close,volume"
    )
    assert (tmp_path / "runtime" / "strategy-versions" / "versions" / f"{report['strategy_version_id']}.json").is_file()


def test_trusted_rsi_route_runs_real_backtrader_and_versions_template(tmp_path, monkeypatch):
    import backtest.runner as runner

    run_dir = tmp_path / "runtime" / "runs" / "run_trusted_rsi"
    run_dir.mkdir(parents=True)
    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-19").date())
    dates = pd.DatetimeIndex([pd.Timestamp(session.session_date).tz_localize("UTC") for session in schedule.sessions])
    closes = [10, 11, 10, 9, 10, 11, 10, 9, 8, 9, 10, 11]
    bars = pd.DataFrame({
        "open": [value + 0.25 for value in closes],
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
        "volume": [100_000] * len(closes),
    }, index=dates)
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(bars, symbol))
    config = _backtrader_config()
    spec = config["strategy_spec"]
    spec.update(strategy_id="route_test_rsi", template_id="rsi_threshold", parameters={
        "period": 2, "entry_below": 40, "exit_above": 60,
    })
    config.update(start_date="2025-02-03", end_date="2025-02-19")

    report = run_backtrader_backend(config, run_dir)

    assert report["template_id"] == "rsi_threshold"
    assert report["parameters"] == {"period": 2, "entry_below": 40.0, "exit_above": 60.0}
    assert report["strategy_version_id"] == report["strategy_version_hash"]
    assert report["execution_integrity"]["status"] == "verified"
    assert report["data_manifest"]["warmup_end"] == "2025-02-05"
    assert report["data_manifest"]["evaluation_start"] == "2025-02-06"
    assert [row["side"] for row in report["orders"]] == ["BUY", "SELL", "BUY", "SELL"]


def test_trusted_route_does_not_publish_report_when_replay_mismatches(tmp_path, monkeypatch):
    import backtest.runner as runner
    from src.bt_runtime.report_replay import ReportReplayMismatch

    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-19").date())
    dates = pd.DatetimeIndex([pd.Timestamp(session.session_date).tz_localize("UTC") for session in schedule.sessions])
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    bars = pd.DataFrame({
        "open": [value + 0.25 for value in closes], "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes], "close": closes, "volume": [100_000] * len(closes),
    }, index=dates)
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(bars, symbol))
    monkeypatch.setattr(
        "src.bt_runtime.report_replay.verify_backtest_report_replay",
        lambda *args, **kwargs: (_ for _ in ()).throw(ReportReplayMismatch("fixture mismatch")),
    )
    run_dir = tmp_path / "runtime" / "runs" / "run_replay_mismatch"
    run_dir.mkdir(parents=True)
    config = _backtrader_config()
    config.update(start_date="2025-02-03", end_date="2025-02-19")

    with pytest.raises(ReportReplayMismatch, match="fixture mismatch"):
        runner.run_backtrader_backend(config, run_dir)
    assert not (run_dir / "artifacts" / "execution_report.json").exists()


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("unknown_field", "outside the verified execution schema"),
        ("remove_target_shares", "is missing target_shares"),
        ("target_shares", "differs at target_shares"),
        ("sizing_requested_qty", "differs at sizing_requested_qty"),
        ("accepted_qty", "differs at accepted_qty"),
        ("sizing_reason", "differs at orders"),
    ],
)
def test_trusted_route_replay_rejects_unverified_order_fields(tmp_path, monkeypatch, tamper, message):
    import backtest.runner as runner
    from src.bt_runtime.report_replay import ReportReplayMismatch, verify_backtest_report_replay as verify

    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-19").date())
    dates = pd.DatetimeIndex([pd.Timestamp(session.session_date).tz_localize("UTC") for session in schedule.sessions])
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    bars = pd.DataFrame({
        "open": [value + 0.25 for value in closes], "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes], "close": closes, "volume": [100_000] * len(closes),
    }, index=dates)
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(bars, symbol))

    def verify_with_forged_field(report, replay, **kwargs):
        if tamper == "unknown_field":
            report["orders"][0]["broker_reported_realized_pnl"] = 1_000_000.0
        elif tamper == "remove_target_shares":
            del report["orders"][0]["target_shares"]
        elif tamper == "target_shares":
            report["orders"][0]["target_shares"] += 1
        elif tamper == "sizing_requested_qty":
            report["orders"][0]["sizing_requested_qty"] += 1
        elif tamper == "accepted_qty":
            report["orders"][0]["accepted_qty"] += 1
        else:
            report["orders"][0]["sizing_reason"] = "forged_reason"
        return verify(report, replay, **kwargs)

    monkeypatch.setattr("src.bt_runtime.report_replay.verify_backtest_report_replay", verify_with_forged_field)
    run_dir = tmp_path / "runtime" / "runs" / "run_forged_order_field"
    run_dir.mkdir(parents=True)
    config = _backtrader_config()
    config.update(start_date="2025-02-03", end_date="2025-02-19")

    with pytest.raises(ReportReplayMismatch, match=message):
        runner.run_backtrader_backend(config, run_dir)
    assert not (run_dir / "artifacts" / "execution_report.json").exists()


def test_trusted_route_rejects_timezone_that_does_not_match_xnys(tmp_path, monkeypatch):
    import backtest.runner as runner

    config = _backtrader_config()
    config["strategy_spec"]["bar_policy"]["timezone"] = "UTC"
    monkeypatch.setattr(
        runner,
        "fetch_exact_yfinance_daily",
        lambda *_: pytest.fail("provider must not be called for an unsupported calendar timezone"),
    )
    run_dir = tmp_path / "runtime" / "runs" / "wrong_timezone"
    run_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="require timezone 'America/New_York'"):
        runner.run_backtrader_backend(config, run_dir)


@pytest.mark.parametrize("missing_index", [0, 5, -1], ids=["first-session", "interior-session", "last-session"])
def test_trusted_route_rejects_any_missing_requested_session(tmp_path, monkeypatch, missing_index):
    from types import SimpleNamespace

    import backtest.runner as runner

    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-19").date())
    sessions = list(schedule.sessions)
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    omitted = missing_index % len(sessions)
    retained = [index for index in range(len(sessions)) if index != omitted]
    frame = pd.DataFrame(
        {
            "open": [closes[index] + 0.25 for index in retained],
            "high": [closes[index] + 1 for index in retained],
            "low": [closes[index] - 1 for index in retained],
            "close": [closes[index] for index in retained],
            "volume": [100_000] * len(retained),
        },
        index=pd.DatetimeIndex([
            pd.Timestamp(sessions[index].session_date).tz_localize("UTC") for index in retained
        ]),
    )
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(frame, symbol))
    config = _backtrader_config()
    config.update(start_date="2025-02-03", end_date="2025-02-19")
    run_dir = tmp_path / "runtime" / "runs" / f"missing_{omitted}"
    run_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="provider sessions do not match pinned XNYS calendar"):
        runner.run_backtrader_backend(config, run_dir)
    assert not (run_dir / "artifacts" / "execution_report.json").exists()


def test_trusted_route_rejects_nonmidnight_daily_label(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import backtest.runner as runner

    schedule = build_xnys_schedule(pd.Timestamp("2025-02-03").date(), pd.Timestamp("2025-02-03").date())
    session = schedule.sessions[0]
    frame = pd.DataFrame(
        {"open": [10], "high": [11], "low": [9], "close": [10], "volume": [100]},
        index=pd.DatetimeIndex([pd.Timestamp(session.session_date, tz="UTC") + pd.Timedelta(hours=1)]),
    )
    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", lambda symbol, _start, _end: _fake_yfinance_result(frame, symbol))
    run_dir = tmp_path / "runtime" / "runs" / "nonmidnight"
    run_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="midnight session-date labels"):
        runner.run_backtrader_backend(_backtrader_config(), run_dir)


def test_run_detail_api_exposes_execution_backend_version_and_report(tmp_path):
    import api_server

    run_dir = tmp_path / "run_api_report"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (run_dir / "state.json").write_text('{"status":"success"}', encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps({"execution_backend": "backtrader", "strategy_spec": _spec()}),
        encoding="utf-8",
    )
    report = {
        "execution_backend": "backtrader",
        "execution_mode": "backtest",
        "strategy_version_id": "a" * 64,
        "strategy_version_hash": "a" * 64,
        "orders": [{"side": "BUY", "requested_qty": 4}],
        "fills": [{"side": "BUY", "qty": 4, "price": 10.0}],
        "data_manifest_hash": "b" * 64,
        "mode": "provider_import_with_unverified_provenance",
    }
    (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")

    response = api_server._build_response_from_run_dir(run_dir, elapsed=0.0)

    assert response.execution_backend == "backtrader"
    assert response.execution_mode == "backtest"
    assert response.strategy_version_id == "a" * 64
    assert response.strategy_version_hash == "a" * 64
    assert response.execution_report == report
    assert isinstance(response.strategy_spec, dict)
