"""Tests for creating persisted trusted-template Backtrader runs."""

from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server


def test_python_strategy_failure_api_projection_excludes_untrusted_exception_text():
    from src.api.runs_routes import _public_python_strategy_failure
    from src.bt_runtime.strategies.python_upload import PythonStrategyRunError

    failure = PythonStrategyRunError(
        "raw worker failure SECRET-MARKER", event_seq=3,
        public_failure={
            "category": "strategy_runtime_error", "error_type": "NameError",
            "source_line": 7, "message": "SECRET-MARKER",
        },
    )
    public = _public_python_strategy_failure(failure)

    assert public == {
        "status": "failed", "error_code": "strategy_runtime_error", "event_seq": 3,
        "error_type": "NameError", "source_line": 7,
        "message": "Bar 3: Strategy code raised an error (NameError) on source line 7",
    }
    assert "SECRET-MARKER" not in json.dumps(public)


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    import pandas as pd
    from backtest import runner
    from src.bt_runtime.calendars import build_xnys_schedule

    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path / "runs")

    def deterministic_market_data(symbol, start, end):
        schedule = build_xnys_schedule(start, end)
        index = pd.DatetimeIndex([pd.Timestamp(item.session_date) for item in schedule.sessions])
        values = [100.0 + i * 0.1 for i in range(len(index))]
        frame = pd.DataFrame({
            "open": values, "high": [value + 1 for value in values],
            "low": [value - 1 for value in values], "close": values,
            "volume": [1000.0] * len(values),
        }, index=index)
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
                    "symbol": symbol, "start_date": start.isoformat(),
                    "end_date_inclusive": end.isoformat(), "interval": "1D", "fields": None,
                },
                "retrieved_at": datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
                "loader_cache": "miss",
                "loader_output_sha256": canonical_loader_frame_sha256(frame),
                "identity_authentication": "UNVERIFIED",
                "point_in_time_availability": "UNVERIFIED",
                "adjustment_semantics": "UNVERIFIED",
                "broker_contract_mapping": "UNVERIFIED",
            },
        )

    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", deterministic_market_data)
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _payload(**overrides):
    return {
        "symbol": "AAPL",
        "instrument_id": "AAPL.US",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "source": "yfinance",
        "strategy_description": "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA.",
        "allocation_fraction": 0.9,
        **overrides,
    }


def _run_directories(runs_dir: Path) -> list[Path]:
    return [path for path in runs_dir.iterdir() if path.is_dir()] if runs_dir.exists() else []


def _preview_python_upload(client: TestClient, payload: dict) -> dict:
    preview = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["supported"] is True, result
    return {**payload, "preview_token": result["preview_token"]}


def test_create_backtest_persists_bounded_strategy_and_report(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from backtest import runner

    fetch = runner.fetch_exact_yfinance_daily
    fetch_calls = []

    def count_fetch(symbol, start, end):
        fetch_calls.append((symbol, start, end))
        return fetch(symbol, start, end)

    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", count_fetch)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    assert len(preview["summary"]["market_data_sha256"]) == 64
    assert len(preview["summary"]["data_manifest_sha256"]) == 64
    assert len(fetch_calls) == 1

    def fake_runner(config, run_dir):
        assert config["execution_backend"] == "backtrader"
        assert config["strategy_spec"]["template_id"] == "sma_cross"
        assert config["strategy_spec"]["instrument_ids"] == ["AAPL.US"]
        assert config["strategy_spec"]["parameters"] == {"fast_period": 10, "slow_period": 30}
        assert config["slippage_fraction"] == 0.0
        assert config["_frozen_market_data"]["data_sha256"]
        assert config["_frozen_market_data"]["manifest_sha256"]
        report = {
            "execution_backend": "backtrader",
            "execution_mode": "backtest",
            "final_value": 101_250.0,
            "total_fees": 12.5,
            "metrics": {"total_return": 0.0125},
        }
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    response = client.post("/runs/backtests", json={**_payload(), "preview_token": preview["preview_token"]})

    assert response.status_code == 200
    assert len(fetch_calls) == 1, "confirmation must consume the preview's frozen bars without refetching"
    result = response.json()
    assert result["status"] == "success"
    assert result["execution_report"]["final_value"] == 101_250.0
    run_dir = tmp_path / "runs" / result["run_id"]
    assert json.loads((run_dir / "state.json").read_text())["status"] == "success"
    persisted_config = json.loads((run_dir / "config.json").read_text())
    assert persisted_config["source"] == "yfinance"
    assert persisted_config["execution_backend"] == "backtrader"
    detail = client.get(f"/runs/{result['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["execution_report"]["final_value"] == 101_250.0
    assert detail.json()["execution_report_integrity"] == {"status": "verified"}
    sidecar = json.loads((run_dir / "artifacts" / "execution_report.integrity.json").read_text())
    assert sidecar["schema_version"] == "backtrader-execution-report-integrity/1"
    assert sidecar["artifact_path"] == "artifacts/execution_report.json"
    assert sidecar["size_bytes"] == (run_dir / "artifacts" / "execution_report.json").stat().st_size
    assert detail.json()["run_context"]["codes"] == ["AAPL"]
    listed = client.get("/runs")
    assert listed.status_code == 200
    assert listed.json()[0]["run_id"] == result["run_id"]
    assert listed.json()[0]["codes"] == ["AAPL"]
    assert listed.json()[0]["start_date"] == "2024-01-01"
    replay = client.post("/runs/backtests", json={**_payload(), "preview_token": preview["preview_token"]})
    assert replay.status_code == 409


def test_nl2_preview_confirm_and_report_read_run_real_backtrader(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from backtest import runner

    original_fetch = runner.fetch_exact_yfinance_daily
    fetch_calls = []

    def count_fetch(symbol, start, end):
        fetch_calls.append((symbol, start, end))
        return original_fetch(symbol, start, end)

    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", count_fetch)
    preview_response = client.post("/runs/backtests/preview", json=_payload())
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["supported"] is True

    created = client.post(
        "/runs/backtests",
        json={**_payload(), "preview_token": preview["preview_token"]},
    )
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["status"] == "success"
    assert len(fetch_calls) == 1, "confirmation must consume the preview's frozen market snapshot"

    report = result["execution_report"]
    assert report["execution_backend"] == "backtrader"
    assert report["execution_mode"] == "backtest"
    assert report["engine"].startswith("Backtrader ")
    assert len(report["strategy_version_hash"]) == 64
    assert report["strategy_version_id"] == report["strategy_version_hash"]
    assert report["execution_integrity"]["status"] == "verified"

    run_dir = tmp_path / "runs" / result["run_id"]
    artifacts = run_dir / "artifacts"
    market_data_path = artifacts / "market_data.csv"
    manifest_path = artifacts / "data_manifest.json"
    assert market_data_path.is_file()
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["file_sha256"] == hashlib.sha256(market_data_path.read_bytes()).hexdigest()
    observed = manifest["download_parameters"]["host_observed_provider_fetch"]
    assert observed["requested_provider"] == observed["actual_loader_identity"] == "yfinance"
    assert observed["retrieved_at"]
    assert observed["loader_cache"] in {"hit", "miss"}
    assert len(observed["loader_output_sha256"]) == 64
    assert len(observed["normalized_ohlcv_sha256"]) == 64
    assert observed["snapshot_csv_sha256"] == manifest["file_sha256"]
    assert observed["identity_authentication"] == "UNVERIFIED"
    assert observed["point_in_time_availability"] == "UNVERIFIED"
    assert observed["adjustment_semantics"] == "UNVERIFIED"
    assert observed["broker_contract_mapping"] == "UNVERIFIED"
    assert manifest == report["data_manifest"]
    assert report["data_manifest_hash"] == report["provenance"]["data_manifest_hash"]
    assert (artifacts / "strategy_version.json").is_file()
    assert json.loads((artifacts / "execution_report.integrity.json").read_text())["schema_version"] == (
        "backtrader-execution-report-integrity/1"
    )

    detail = client.get(f"/runs/{result['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["execution_report_integrity"] == {"status": "verified"}
    assert detail.json()["execution_report"]["strategy_version_hash"] == report["strategy_version_hash"]


def test_ibkr_preview_freezes_readonly_provider_bars_and_runs_real_backtrader(tmp_path: Path, monkeypatch):
    import pandas as pd
    from backtest import runner
    from src.bt_runtime.calendars import build_xnys_schedule
    from src.execution.ibkr_daily_provider import IbkrDailyResult

    client = _client(tmp_path, monkeypatch)
    fetch_calls = []

    def fetch(symbol, start, end, *, instrument_id, profile_id):
        fetch_calls.append((symbol, start, end, instrument_id, profile_id))
        schedule = build_xnys_schedule(start, end)
        index = pd.DatetimeIndex([pd.Timestamp(item.session_date) for item in schedule.sessions])
        values = [100.0 + i * 0.1 for i in range(len(index))]
        frame = pd.DataFrame({
            "open": values, "high": [value + 1 for value in values],
            "low": [value - 1 for value in values], "close": values,
            "volume": [1000.0] * len(values),
        }, index=index)
        return IbkrDailyResult(
            symbol=symbol,
            frame=frame,
            provenance={
                "schema": "host-observed-provider-fetch/1",
                "requested_provider": "ibkr",
                "actual_loader_identity": "ibkr-local-tws-api",
                "profile_id": profile_id,
                "environment": "live",
                "retrieved_at": datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
                "qualified_contract": {
                    "symbol": "AAPL", "sec_type": "STK", "currency": "USD",
                    "con_id": 265598, "exchange": "SMART", "primary_exchange": "NASDAQ",
                },
                "identity_authentication": "IBKR local API session; selected read-only profile asserted",
                "point_in_time_availability": "UNVERIFIED",
                "adjustment_semantics": "TRADES bars; IBKR adjustment treatment unverified",
                "broker_contract_mapping": "qualified conId recorded; application instrument mapping caller-declared",
            },
        )

    monkeypatch.setattr(runner, "fetch_exact_ibkr_daily", fetch)
    payload = _payload(source="ibkr", ibkr_profile_id="ibkr-live-local-readonly")
    preview_response = client.post("/runs/backtests/preview", json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["supported"] is True
    assert preview["summary"]["market_data_provider"] == "ibkr"
    assert preview["summary"]["ibkr_profile_id"] == "ibkr-live-local-readonly"
    assert len(fetch_calls) == 1

    created = client.post(
        "/runs/backtests",
        json={**payload, "preview_token": preview["preview_token"]},
    )
    assert created.status_code == 200, created.text
    assert len(fetch_calls) == 1, "confirmation must use the frozen IBKR bars without reconnecting/refetching"
    report = created.json()["execution_report"]
    assert report["execution_backend"] == "backtrader"
    assert report["execution_integrity"]["status"] == "verified"
    assert report["data_manifest"]["provider"] == "ibkr"
    assert report["data_manifest"]["download_parameters"]["host_observed_provider_fetch"]["qualified_contract"]["con_id"] == 265598


def test_ibkr_history_can_prepare_python_upload_csv_and_pinned_schedule(tmp_path: Path, monkeypatch):
    import pandas as pd
    from backtest import runner
    from src.bt_runtime.calendars import build_xnys_schedule
    from src.execution.ibkr_daily_provider import IbkrDailyResult

    client = _client(tmp_path, monkeypatch)
    requested = []

    def fetch(symbol, start, end, *, instrument_id, profile_id, port=None):
        requested.append((symbol, start, end, instrument_id, profile_id, port))
        schedule = build_xnys_schedule(start, end)
        index = pd.DatetimeIndex([pd.Timestamp(item.session_date) for item in schedule.sessions])
        values = [100.0 + i for i in range(len(index))]
        frame = pd.DataFrame({
            "open": values, "high": [value + 1 for value in values],
            "low": [value - 1 for value in values], "close": values,
            "volume": [1000.0] * len(values),
        }, index=index)
        return IbkrDailyResult(
            symbol=symbol,
            frame=frame,
            provenance={
                "requested_provider": "ibkr", "profile_id": profile_id,
                "environment": "live", "retrieved_at": "2025-01-09T12:00:00Z",
                "qualified_contract": {"symbol": "AAPL", "con_id": 265598},
            },
        )

    monkeypatch.setattr(runner, "fetch_exact_ibkr_daily", fetch)
    response = client.post("/runs/market-data/ibkr/python-upload-preview", json={
        "symbol": "AAPL", "instrument_id": "AAPL.US",
        "profile_id": "ibkr-live-local-readonly",
        "port": 4001,
        "warmup_start": "2025-01-02", "warmup_end": "2025-01-03",
        "evaluation_start": "2025-01-06", "evaluation_end": "2025-01-08",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["supported"] is True, result
    assert requested == [("AAPL", date(2025, 1, 2), date(2025, 1, 8), "AAPL.US", "ibkr-live-local-readonly", 4001)]
    assert result["market_data_metadata"]["provider"] == "ibkr"
    assert result["market_data_metadata"]["calendar_schedule_sha256"]
    assert len(result["expected_sessions"]) == 5
    assert result["bar_count"] == 5

    upload_preview = client.post("/runs/backtests/python-upload/preview", json={
        "strategy_id": "ibkr_python_fixture",
        "instrument_id": "AAPL.US",
        "strategy_source": "def on_event(event, state):\n    return {'state': state, 'order_proposals': []}\n",
        "market_data_csv": result["market_data_csv"],
        "market_data_metadata": result["market_data_metadata"],
        "expected_sessions": result["expected_sessions"],
        "initial_cash": 10000,
        "commission_rate": 0.001,
    })
    assert upload_preview.status_code == 200, upload_preview.text
    assert upload_preview.json()["supported"] is True, upload_preview.json()
    assert upload_preview.json()["data_manifest"]["provider"] == "ibkr"


def test_report_read_rejects_legacy_report_without_sidecar_silently_claiming_verification(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    run_dir = tmp_path / "runs" / "legacy_report"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (run_dir / "config.json").write_text(json.dumps({"execution_backend": "backtrader"}), encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
    (artifacts / "execution_report.json").write_text(
        json.dumps({"execution_backend": "backtrader", "final_value": 12}), encoding="utf-8"
    )

    detail = client.get("/runs/legacy_report")

    assert detail.status_code == 200, detail.text
    assert detail.json()["execution_report"]["final_value"] == 12
    assert detail.json()["execution_report_integrity"] == {"status": "unverified_legacy"}


def test_report_sidecar_remains_authoritative_if_config_marker_is_removed(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from backtest import runner

    def fake_runner(config, run_dir):
        report = {"execution_backend": "backtrader", "execution_mode": "backtest", "final_value": 42}
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    created = client.post(
        "/runs/backtests", json={**_payload(), "preview_token": preview["preview_token"]}
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    config_path = tmp_path / "runs" / run_id / "config.json"
    config = json.loads(config_path.read_text())
    config.pop("execution_report_integrity_schema")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    detail = client.get(f"/runs/{run_id}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["execution_report_integrity"] == {"status": "verified"}


def test_report_read_rejects_unknown_integrity_schema_marker(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from backtest import runner

    def fake_runner(config, run_dir):
        report = {"execution_backend": "backtrader", "execution_mode": "backtest", "final_value": 42}
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    created = client.post(
        "/runs/backtests", json={**_payload(), "preview_token": preview["preview_token"]}
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    config_path = tmp_path / "runs" / run_id / "config.json"
    config = json.loads(config_path.read_text())
    config["execution_report_integrity_schema"] = "unsupported/99"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    detail = client.get(f"/runs/{run_id}")

    assert detail.status_code == 409
    assert detail.json()["detail"]["code"] == "execution_report_integrity_failed"


def test_verified_report_response_uses_same_snapshot_as_integrity_check(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from src.bt_runtime import report_artifact_integrity as integrity

    run_dir = tmp_path / "runs" / "report_replacement_race"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    report_path = artifacts / "execution_report.json"
    original_report = {"execution_backend": "backtrader", "final_value": 42}
    report_path.write_text(json.dumps(original_report), encoding="utf-8")
    integrity.write_report_integrity_sidecar(report_path)
    (run_dir / "config.json").write_text(
        json.dumps({"execution_backend": "backtrader", "execution_report_integrity_schema": integrity.SCHEMA_VERSION}),
        encoding="utf-8",
    )
    (run_dir / "state.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")

    load_snapshot = integrity.load_verified_report

    def replace_after_snapshot(path):
        parsed, error = load_snapshot(path)
        Path(path).write_text(
            json.dumps({"execution_backend": "backtrader", "final_value": 999}),
            encoding="utf-8",
        )
        return parsed, error

    monkeypatch.setattr(integrity, "load_verified_report", replace_after_snapshot)

    detail = client.get("/runs/report_replacement_race")

    assert detail.status_code == 200, detail.text
    assert detail.json()["execution_report_integrity"] == {"status": "verified"}
    assert detail.json()["execution_report"]["final_value"] == 42


@pytest.mark.parametrize(
    "tamper_mode",
    ["replace", "truncate", "delete_report", "delete_sidecar", "replace_sidecar"],
)
def test_report_read_fails_closed_when_integrity_binding_is_broken(tmp_path: Path, monkeypatch, tamper_mode: str):
    client = _client(tmp_path, monkeypatch)
    from backtest import runner

    def fake_runner(config, run_dir):
        report = {"execution_backend": "backtrader", "execution_mode": "backtest", "final_value": 42}
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    created = client.post(
        "/runs/backtests", json={**_payload(), "preview_token": preview["preview_token"]}
    )
    assert created.status_code == 200, created.text
    run_dir = tmp_path / "runs" / created.json()["run_id"]
    report_path = run_dir / "artifacts" / "execution_report.json"
    sidecar_path = run_dir / "artifacts" / "execution_report.integrity.json"

    if tamper_mode == "replace":
        report_path.write_text(json.dumps({"execution_backend": "backtrader", "final_value": 43}), encoding="utf-8")
    elif tamper_mode == "truncate":
        report_path.write_bytes(report_path.read_bytes()[:12])
    elif tamper_mode == "delete_report":
        report_path.unlink()
    elif tamper_mode == "delete_sidecar":
        sidecar_path.unlink()
    else:
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["sha256"] = "0" * 64
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    detail = client.get(f"/runs/{created.json()['run_id']}")

    assert detail.status_code == 409
    assert detail.json()["detail"]["code"] == "execution_report_integrity_failed"
    assert detail.json()["detail"]["status"] == "invalid"
    assert detail.json()["detail"]["error"]


def test_create_backtest_rejects_unsafe_symbols_and_unsupported_strategy_conditions(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    unsafe = client.post("/runs/backtests", json=_payload(symbol="../AAPL"))
    unsupported = client.post(
        "/runs/backtests",
        json=_payload(strategy_description="10日均线上穿30日均线且RSI低于20买入"),
    )

    assert unsafe.status_code == 422
    assert unsupported.status_code == 422
    assert not (tmp_path / "runs").exists()


def test_strategy_preview_is_read_only_and_returns_a_bound_rule_card(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    preview = client.post("/runs/backtests/preview", json=_payload())

    assert preview.status_code == 200
    result = preview.json()
    assert result["supported"] is True
    assert "clarification_form" not in result
    assert len(result["review_hash"]) == 64
    assert result["strategy_spec"]["parameters"] == {"fast_period": 10, "slow_period": 30}
    assert len(result["template_source_sha256"]) == 64
    assert result["compiler_version"] == "vibe-trusted-templates/2.0"
    assert result["summary"]["strategy_budget"] == 40_000
    assert result["summary"]["fee_reserve"] == 25
    assert result["summary"]["slippage_fraction"] == 0.0
    assert {key: result["summary"][key] for key in (
        "symbol", "instrument_id", "source", "start_date", "end_date", "allocation_fraction",
    )} == {
        "symbol": "AAPL",
        "instrument_id": "AAPL.US",
        "source": "yfinance",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "allocation_fraction": 0.9,
    }
    assert _run_directories(tmp_path / "runs") == []

    unsupported = client.post(
        "/runs/backtests/preview",
        json=_payload(strategy_description="Buy when RSI is below 20"),
    )
    assert unsupported.status_code == 200
    assert unsupported.json()["supported"] is False
    assert "explicit period" in unsupported.json()["clarification"]
    assert unsupported.json()["clarification_form"]["status"] == "needs_clarification"
    assert {field["name"] for field in unsupported.json()["clarification_form"]["missing_fields"]} >= {
        "period", "entry_below", "exit_above",
    }
    assert _run_directories(tmp_path / "runs") == []


def test_strategy_preview_returns_structured_sma_clarification(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/runs/backtests/preview", json=_payload(
        strategy_description="Use a fast and slow SMA crossover, long only."
    ))
    assert response.status_code == 200
    body = response.json()
    form = body["clarification_form"]
    assert body["supported"] is False
    assert form["schema_version"] == "strategy-clarification/1"
    assert form["status"] == "needs_clarification"
    assert {field["name"] for field in form["missing_fields"]} >= {"fast_period", "slow_period"}
    assert "preview_token" not in body
    assert "strategy_spec" not in body


def test_strategy_clarification_transcript_is_durable_idempotent_and_context_bound(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    initial = client.post("/runs/backtests/preview", json=_payload(
        strategy_description="Use a fast and slow SMA crossover, long only."
    ))
    assert initial.status_code == 200, initial.text
    first_cursor = initial.json()["clarification_transcript"]
    assert first_cursor["turn_count"] == 1

    turn = {
        **_payload(strategy_description="Use a fast and slow SMA crossover, long only.\n\nUser clarification: fast 10 days, slow 30 days"),
        "clarification_transcript_id": first_cursor["transcript_id"],
        "clarification_turn_id": "aa87dc27-8765-4075-a385-bc020e65fdc0",
        "clarification_parent_turn_id": first_cursor["turn_id"],
        "clarification_answer": "fast 10 days, slow 30 days",
    }
    appended = client.post("/runs/backtests/preview", json=turn)
    assert appended.status_code == 200, appended.text
    second_cursor = appended.json()["clarification_transcript"]
    assert second_cursor == {
        "transcript_id": first_cursor["transcript_id"],
        "turn_id": turn["clarification_turn_id"],
        "turn_count": 2,
    }

    replay = client.post("/runs/backtests/preview", json=turn)
    assert replay.status_code == 200, replay.text
    assert replay.json()["clarification_transcript"] == second_cursor

    reused_id = {**turn, "clarification_answer": "fast 8 days, slow 30 days"}
    reused_id["strategy_description"] = (
        "Use a fast and slow SMA crossover, long only.\n\nUser clarification: fast 8 days, slow 30 days"
    )
    assert client.post("/runs/backtests/preview", json=reused_id).status_code == 409

    stale_parent = {
        **turn,
        "clarification_turn_id": "d2f5bb34-8269-475d-8347-45bfaf50b20f",
        "clarification_answer": "one more detail",
        "strategy_description": turn["strategy_description"] + "\n\nUser clarification: one more detail",
    }
    assert client.post("/runs/backtests/preview", json=stale_parent).status_code == 409

    changed_context = {
        **turn,
        "symbol": "MSFT",
    }
    assert client.post("/runs/backtests/preview", json=changed_context).status_code == 409


def test_strategy_preview_marks_explicit_unsupported_condition_without_fields(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/runs/backtests/preview", json=_payload(
        strategy_description="Buy on a 10-day SMA cross with a stop loss."
    ))
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is False
    assert body["clarification_form"]["status"] == "unsupported"
    assert body["clarification_form"]["missing_fields"] == []


def test_strategy_preview_localizes_structured_clarification_for_chinese_request(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/runs/backtests/preview", json=_payload(
        strategy_description="我想用 RSI 下穿阈值买入，再上穿退出"
    ))
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is False
    assert body["clarification_form"]["status"] == "needs_clarification"
    assert "请" in body["clarification_form"]["question"]
    assert "RSI 周期" in {field["label"] for field in body["clarification_form"]["missing_fields"]}
    assert "preview_token" not in body
    assert "preview_token" not in body
    assert "strategy_spec" not in body


def test_strategy_preview_rejects_client_supplied_strategy_fields(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/runs/backtests/preview", json={
        **_payload(),
        "strategy_spec": {"template_id": "sma_cross", "parameters": {"fast_period": 1, "slow_period": 2}},
        "evidence": [],
        "clarification_session": "forged",
    })
    assert response.status_code == 422


def test_rsi_preview_and_create_reuse_the_same_explicit_frozen_spec(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(strategy_description="Buy when 14-period RSI crosses below 30; exit when RSI crosses above 70.")
    preview = client.post("/runs/backtests/preview", json=payload)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value["supported"] is True
    assert value["strategy_spec"]["template_id"] == "rsi_threshold"
    assert value["strategy_spec"]["parameters"] == {"period": 14, "entry_below": 30.0, "exit_above": 70.0}

    def fake_runner(config, run_dir):
        assert config["strategy_spec"] == value["strategy_spec"]
        report = {"execution_backend": "backtrader", "execution_mode": "backtest",
                  "final_value": 100_000.0, "total_fees": 0.0, "metrics": {}}
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    from backtest import runner

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    created = client.post("/runs/backtests", json={**payload, "preview_token": value["preview_token"]})
    assert created.status_code == 200, created.text


def test_model_generated_rsi_spec_is_frozen_between_preview_and_create(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    import src.execution.strategy_design as strategy_design

    calls = []

    def generated_once(description, *, intent_generator=None):
        calls.append(description)
        return {"period": 8, "entry_below": 25.0, "exit_above": 65.0}

    monkeypatch.setattr(strategy_design, "configured_rsi_model_intent", generated_once)
    payload = _payload(strategy_description="Use RSI with an eight-day lookback, enter on a drop through 25 and exit on a rise through 65.")
    preview_response = client.post("/runs/backtests/preview", json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["supported"] is True
    expected_spec = preview["strategy_spec"]
    assert expected_spec["template_id"] == "rsi_threshold"
    assert expected_spec["parameters"] == {"period": 8, "entry_below": 25.0, "exit_above": 65.0}
    assert calls == [payload["strategy_description"]]

    def generation_must_not_repeat(*_args, **_kwargs):
        raise AssertionError("RSI strategy extraction was repeated after preview")

    monkeypatch.setattr(strategy_design, "configured_rsi_model_intent", generation_must_not_repeat)

    def fake_runner(config, run_dir):
        assert config["strategy_spec"] == expected_spec
        report = {"execution_backend": "backtrader", "execution_mode": "backtest",
                  "final_value": 100_000.0, "total_fees": 0.0, "metrics": {}}
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    from backtest import runner

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    created = client.post("/runs/backtests", json={**payload, "preview_token": preview["preview_token"]})
    assert created.status_code == 200, created.text


def test_reviewed_backtest_rejects_changed_inputs_before_creating_a_run(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    missing = client.post("/runs/backtests", json=_payload())
    changed = _payload(symbol="MSFT", preview_token=preview["preview_token"])

    response = client.post("/runs/backtests", json=changed)
    assert missing.status_code == 409
    assert response.status_code == 409
    assert "fresh strategy preview" in response.json()["detail"]
    assert _run_directories(tmp_path / "runs") == []


def test_review_token_expires_when_trusted_template_identity_changes(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    from src.bt_runtime.strategies import sma as sma_runtime

    monkeypatch.setattr(sma_runtime, "TRUSTED_TEMPLATE_SOURCE_SHA256", "0" * 64)
    response = client.post(
        "/runs/backtests",
        json={**_payload(), "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 409
    assert _run_directories(tmp_path / "runs") == []


def test_trusted_runner_uses_exact_preview_snapshot_without_refetching(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    preview = client.post("/runs/backtests/preview", json=_payload()).json()
    token_hash = __import__("hashlib").sha256(preview["preview_token"].encode("ascii")).hexdigest()
    import sqlite3

    database = tmp_path / "runs" / ".backtest_preview_authorizations.sqlite3"
    with sqlite3.connect(database) as connection:
        snapshot_json = connection.execute(
            "SELECT market_snapshot_json FROM preview_authorizations WHERE token_hash=?", (token_hash,)
        ).fetchone()[0]
    snapshot = json.loads(snapshot_json)

    from backtest import runner

    def provider_must_not_be_called(_config):
        raise AssertionError("runner refetched provider data after preview")

    monkeypatch.setattr(runner, "fetch_exact_yfinance_daily", provider_must_not_be_called)
    run_dir = tmp_path / "runs" / "bt_frozen_snapshot"
    run_dir.mkdir()
    template_identity = preview
    config = {
        "codes": ["AAPL"], "instrument_id": "AAPL.US", "start_date": "2024-01-01",
        "end_date": "2024-12-31", "source": "yfinance", "execution_backend": "backtrader",
        "strategy_budget": 40_000, "fee_reserve": 25, "price_buffer_fraction": 0.01,
        "commission_rate": 0.001, "slippage_fraction": 0.0, "initial_cash": 100_000,
        "strategy_spec": preview["strategy_spec"],
        "expected_strategy_source_hash": template_identity["template_source_sha256"],
        "expected_compiler_version": template_identity["compiler_version"],
    }
    report = runner.run_backtrader_backend(config, run_dir, snapshot)
    artifacts = run_dir / "artifacts"
    assert report["execution_integrity"]["status"] == "verified"
    assert (artifacts / "market_data.csv").read_text(encoding="utf-8") == snapshot["market_data_csv"]
    manifest = json.loads((artifacts / "data_manifest.json").read_text(encoding="utf-8"))
    assert manifest["file_sha256"] == snapshot["data_sha256"]


def test_model_generated_strategy_spec_is_frozen_between_preview_and_create(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    import src.execution.strategy_design as strategy_design

    calls = []

    def generated_once(description):
        calls.append(description)
        return 7, 21

    monkeypatch.setattr(strategy_design, "configured_model_intent", generated_once)
    preview_response = client.post(
        "/runs/backtests/preview",
        json=_payload(strategy_description="Use a 7-day fast and 21-day slow moving average crossover."),
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["strategy_spec"]["parameters"] == {"fast_period": 7, "slow_period": 21}
    assert len(calls) == 1

    # Confirmation must consume the exact reviewed artifact instead of asking
    # the model to generate a potentially different strategy a second time.
    def generation_must_not_repeat(_description):
        raise AssertionError("strategy generation was repeated after preview")

    monkeypatch.setattr(strategy_design, "configured_model_intent", generation_must_not_repeat)

    def fake_runner(config, run_dir):
        assert config["strategy_spec"]["parameters"] == {"fast_period": 7, "slow_period": 21}
        report = {
            "execution_backend": "backtrader",
            "execution_mode": "backtest",
            "final_value": 100_000.0,
            "total_fees": 0.0,
            "metrics": {},
        }
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        (artifacts / "execution_report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    from backtest import runner

    monkeypatch.setattr(runner, "run_backtrader_backend", fake_runner)
    response = client.post(
        "/runs/backtests",
        json={
            **_payload(strategy_description="Use a 7-day fast and 21-day slow moving average crossover."),
            "preview_token": preview["preview_token"],
        },
    )
    assert response.status_code == 200, response.text
    assert calls == ["Use a 7-day fast and 21-day slow moving average crossover."]
    result = response.json()
    persisted = json.loads(
        (tmp_path / "runs" / result["run_id"] / "config.json").read_text(encoding="utf-8")
    )
    assert persisted["strategy_spec"]["parameters"] == {"fast_period": 7, "slow_period": 21}


def test_preview_rejects_model_defaulted_sma_periods_without_issuing_token(tmp_path: Path, monkeypatch):
    import src.execution.strategy_design as strategy_design

    client = _client(tmp_path, monkeypatch)
    defaulted_intent = {
        "fast_period": 10,
        "slow_period": 30,
        "fast_period_evidence": None,
        "slow_period_evidence": None,
        "entry_rule": "fast_sma_crosses_above_slow_sma",
        "exit_rule": "fast_sma_crosses_below_slow_sma",
        "direction": "long_only",
        "unsupported_conditions": [],
        "clarification": None,
    }
    monkeypatch.setattr(
        strategy_design,
        "configured_model_intent",
        lambda description: strategy_design._parse_model_intent(json.dumps(defaulted_intent), description),
    )

    response = client.post(
        "/runs/backtests/preview",
        json=_payload(strategy_description="Use a fast and slow moving average crossover."),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["supported"] is False
    assert "quoted from the user's request" in body["clarification"]
    assert "preview_token" not in body
    assert _run_directories(tmp_path / "runs") == []


def test_sma_description_parser_accepts_supported_chinese_and_english_forms():
    from src.api.runs_routes import _parse_trusted_sma_description

    assert _parse_trusted_sma_description(
        "10日均线上穿30日均线买入，10日均线下穿30日均线卖出"
    ) == (10, 30)
    assert _parse_trusted_sma_description(
        "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA."
    ) == (10, 30)


def test_python_upload_api_uses_separate_route_and_persists_frozen_artifacts(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "a" * 64)

    class FakeSandbox:
        def __init__(self, image):
            self.image = image

        def run_json(self, request):
            return type("Result", (), {"response": {
                "protocol_version": request["protocol_version"], "request_id": request["request_id"],
                "event_id": request["event_id"], "state": request["state"], "order_proposals": [],
            }})()

    import src.execution.python_sandbox as sandbox_module
    monkeypatch.setattr(sandbox_module, "PythonSandbox", FakeSandbox)

    days = [date(2025, 2, day) for day in (3, 4, 5)]
    sessions = []
    lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
    for index, day in enumerate(days):
        opened = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30)
        closed = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21)
        sessions.append({"session_date": day.isoformat(), "open_at": opened.isoformat(), "close_at": closed.isoformat()})
        open_value = 10 + index
        lines.append(
            f"{day.isoformat()},{opened.isoformat()},{closed.isoformat()},"
            f"{(closed + timedelta(minutes=1)).isoformat()},{open_value},{open_value + 1},"
            f"{open_value - 1},{open_value}.5,1000"
        )
    raw_csv = "\n".join(lines) + "\n"
    payload = {
        "strategy_id": "api_python_fixture",
        "instrument_id": "AAPL",
        "strategy_source": "def on_event(event, state):\n    return {'state': state, 'order_proposals': []}\n",
        "market_data_csv": raw_csv,
        "market_data_metadata": {
            "provider": "fixture_vendor", "instrument_id": "AAPL", "currency": "USD",
            "timezone": "America/New_York", "calendar_id": "XNYS", "calendar_version": "4.13.2",
            "availability_model": "completed bar available one minute after close",
            "adjustment_policy": "fixture_unadjusted", "corporate_action_policy": "fixture_none",
            "fetched_at": "2025-02-06T00:00:00Z", "warmup_start": days[0].isoformat(),
            "warmup_end": days[0].isoformat(), "evaluation_start": days[1].isoformat(),
            "evaluation_end": days[2].isoformat(), "download_parameters": {"fixture": True},
        },
        "expected_sessions": sessions,
        "initial_cash": 1000,
        "commission_rate": 0.001,
    }

    rejected_preview = client.post("/runs/backtests/python-upload/preview", json={
        **payload,
        "strategy_source": "import socket\ndef on_event(event, state):\n    return {'state': {}, 'order_proposals': []}\n",
    })
    assert rejected_preview.status_code == 200, rejected_preview.text
    assert rejected_preview.json()["supported"] is False
    assert "allowlist" in rejected_preview.json()["reason"]
    assert _run_directories(tmp_path / "runs") == []
    assert not (tmp_path / "strategy-versions").exists()

    preview_response = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["supported"] is True
    assert preview["summary"]["bar_count"] == 3
    assert preview["summary"]["data_authenticity"] == "caller_declared_unverified"
    assert preview["summary"]["slippage_fraction"] == 0.0
    assert preview["worker_image"] == "sha256:" + "a" * 64
    assert len(preview["source_sha256"]) == 64
    assert len(preview["market_data_sha256"]) == 64
    assert _run_directories(tmp_path / "runs") == []
    assert not (tmp_path / "strategy-versions").exists()

    invalid_calendar_inputs = []
    wrong_holiday = json.loads(json.dumps(payload))
    wrong_holiday["expected_sessions"][0]["session_date"] = "2025-02-17"
    invalid_calendar_inputs.append(wrong_holiday)
    wrong_open = json.loads(json.dumps(payload))
    wrong_open["expected_sessions"][0]["open_at"] = "2025-02-03T15:30:00Z"
    invalid_calendar_inputs.append(wrong_open)
    wrong_early_close = json.loads(json.dumps(payload))
    wrong_early_close["expected_sessions"][0]["early_close"] = True
    invalid_calendar_inputs.append(wrong_early_close)
    wrong_version = json.loads(json.dumps(payload))
    wrong_version["market_data_metadata"]["calendar_version"] = "caller-version"
    invalid_calendar_inputs.append(wrong_version)
    wrong_timezone = json.loads(json.dumps(payload))
    wrong_timezone["market_data_metadata"]["timezone"] = "UTC"
    invalid_calendar_inputs.append(wrong_timezone)
    wrong_schedule_hash = json.loads(json.dumps(payload))
    wrong_schedule_hash["market_data_metadata"]["calendar_schedule_sha256"] = "0" * 64
    invalid_calendar_inputs.append(wrong_schedule_hash)
    for invalid_payload in invalid_calendar_inputs:
        rejected = client.post("/runs/backtests/python-upload/preview", json=invalid_payload)
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["supported"] is False
        assert "XNYS" in rejected.json()["reason"] or "pinned XNYS schedule" in rejected.json()["reason"]
        assert _run_directories(tmp_path / "runs") == []
        assert not (tmp_path / "strategy-versions").exists()

    import sqlite3
    from src.api import runs_routes
    authorization_db = tmp_path / "runs" / ".backtest_preview_authorizations.sqlite3"
    with sqlite3.connect(authorization_db) as connection:
        stored_snapshot = connection.execute(
            "SELECT market_snapshot_json FROM preview_authorizations WHERE consumed_at IS NULL"
        ).fetchone()[0]
    original_preview_capacity = runs_routes._PREVIEW_AUTHORIZATION_MAX_ACTIVE_BYTES
    monkeypatch.setattr(runs_routes, "_PREVIEW_AUTHORIZATION_MAX_ACTIVE_BYTES", len(stored_snapshot.encode("utf-8")) * 2 - 1)
    capacity_response = client.post("/runs/backtests/python-upload/preview", json=payload)
    monkeypatch.setattr(runs_routes, "_PREVIEW_AUTHORIZATION_MAX_ACTIVE_BYTES", original_preview_capacity)
    assert capacity_response.status_code == 429
    assert _run_directories(tmp_path / "runs") == []
    assert not (tmp_path / "strategy-versions").exists()

    changed_after_review = client.post("/runs/backtests/python-upload", json={
        **payload,
        "strategy_source": payload["strategy_source"].replace("order_proposals", "changed_proposals"),
        "preview_token": preview["preview_token"],
    })
    assert changed_after_review.status_code == 409
    assert _run_directories(tmp_path / "runs") == []
    assert not (tmp_path / "strategy-versions").exists()

    changed_cost_after_review = client.post("/runs/backtests/python-upload", json={
        **payload,
        "slippage_fraction": 0.01,
        "preview_token": preview["preview_token"],
    })
    assert changed_cost_after_review.status_code == 409
    assert _run_directories(tmp_path / "runs") == []

    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "b" * 64)
    changed_worker_after_review = client.post("/runs/backtests/python-upload", json={
        **payload,
        "preview_token": preview["preview_token"],
    })
    assert changed_worker_after_review.status_code == 409
    assert _run_directories(tmp_path / "runs") == []
    assert not (tmp_path / "strategy-versions").exists()
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "a" * 64)

    response = client.post("/runs/backtests/python-upload", json={
        "strategy_id": payload["strategy_id"],
        "instrument_id": payload["instrument_id"],
        "strategy_source": payload["strategy_source"],
        "initial_cash": payload["initial_cash"],
        "commission_rate": payload["commission_rate"],
        "slippage_fraction": payload.get("slippage_fraction", 0.0),
        "preview_token": preview["preview_token"],
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["execution_report"]["provenance"]["cost"]["slippage_fraction"] == "0.0"
    assert result["execution_report"]["source_kind"] == "python_upload"
    assert result["execution_report"]["final_position"]["qty"] == 0
    run_dir = tmp_path / "runs" / result["run_id"]
    assert (run_dir / "artifacts" / "market_data.csv").read_text() == raw_csv
    persisted_input = json.loads((run_dir / "artifacts" / "backtest_input.json").read_text())
    expected_persisted_metadata = {**payload["market_data_metadata"], "calendar_schedule_sha256": None}
    assert persisted_input["market_data_metadata"] == expected_persisted_metadata
    assert persisted_input["expected_sessions"] == [
        {**session, "open_at": session["open_at"].replace("+00:00", "Z"),
         "close_at": session["close_at"].replace("+00:00", "Z"), "early_close": False}
        for session in sessions
    ]
    assert json.loads((run_dir / "state.json").read_text())["status"] == "success"
    persisted_config = json.loads((run_dir / "config.json").read_text())
    persisted_worker_config = json.loads((run_dir / "artifacts" / "backtest_run_config.json").read_text())
    assert persisted_config["worker_image"] == "sha256:" + "a" * 64
    assert persisted_worker_config["worker_image"] == "sha256:" + "a" * 64
    assert result["execution_report"]["worker_image"] == "sha256:" + "a" * 64
    detail = client.get(f"/runs/{result['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["execution_report"]["strategy_version_hash"] == result["execution_report"]["strategy_version_hash"]
    assert detail.json()["execution_report_integrity"] == {"status": "verified"}
    assert (run_dir / "artifacts" / "execution_report.integrity.json").is_file()
    assert detail.json()["run_context"]["codes"] == ["AAPL"]
    import hashlib
    authorization_db = tmp_path / "runs" / ".backtest_preview_authorizations.sqlite3"
    consumed_token_hash = hashlib.sha256(preview["preview_token"].encode("ascii")).hexdigest()
    with sqlite3.connect(authorization_db) as connection:
        consumed_snapshot = connection.execute(
            "SELECT market_snapshot_json FROM preview_authorizations WHERE token_hash=?",
            (consumed_token_hash,),
        ).fetchone()[0]
    assert consumed_snapshot is None, "consumed preview snapshot payload should be released"
    replay_confirmation = client.post("/runs/backtests/python-upload", json={
        "strategy_id": payload["strategy_id"],
        "instrument_id": payload["instrument_id"],
        "strategy_source": payload["strategy_source"],
        "initial_cash": payload["initial_cash"],
        "commission_rate": payload["commission_rate"],
        "slippage_fraction": payload.get("slippage_fraction", 0.0),
        "preview_token": preview["preview_token"],
    })
    assert replay_confirmation.status_code == 409
    assert len(_run_directories(tmp_path / "runs")) == 1

    for tamper_mode in ("snapshot_checksum", "review_hash"):
        tamper_preview = client.post("/runs/backtests/python-upload/preview", json=payload).json()
        token_hash = hashlib.sha256(tamper_preview["preview_token"].encode("ascii")).hexdigest()
        with sqlite3.connect(authorization_db) as connection:
            snapshot_json = connection.execute(
                "SELECT market_snapshot_json FROM preview_authorizations WHERE token_hash=?", (token_hash,)
            ).fetchone()[0]
            snapshot = json.loads(snapshot_json)
            if tamper_mode == "snapshot_checksum":
                snapshot["market_data_csv"] += "tampered\n"
            else:
                changed_csv = snapshot["market_data_csv"].replace(",10.5,1000", ",10.6,1000", 1)
                assert changed_csv != snapshot["market_data_csv"]
                snapshot["market_data_csv"] = changed_csv
                unsigned_snapshot = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
                snapshot_bytes = json.dumps(unsigned_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                snapshot["snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
            connection.execute(
                "UPDATE preview_authorizations SET market_snapshot_json=? WHERE token_hash=?",
                (json.dumps(snapshot, sort_keys=True, separators=(",", ":")), token_hash),
            )
        before_tamper_confirmation = len(_run_directories(tmp_path / "runs"))
        tampered_confirmation = client.post("/runs/backtests/python-upload", json={
            "strategy_id": payload["strategy_id"],
            "instrument_id": payload["instrument_id"],
            "strategy_source": payload["strategy_source"],
            "initial_cash": payload["initial_cash"],
            "commission_rate": payload["commission_rate"],
            "slippage_fraction": payload.get("slippage_fraction", 0.0),
            "preview_token": tamper_preview["preview_token"],
        })
        assert tampered_confirmation.status_code == 409, tampered_confirmation.text
        assert len(_run_directories(tmp_path / "runs")) == before_tamper_confirmation

    stale_preview = client.post("/runs/backtests/python-upload/preview", json=payload).json()
    stale_confirmation = client.post("/runs/backtests/python-upload", json={
        "strategy_id": payload["strategy_id"],
        "instrument_id": payload["instrument_id"],
        "strategy_source": payload["strategy_source"],
        "initial_cash": payload["initial_cash"],
        "commission_rate": payload["commission_rate"],
        "slippage_fraction": payload.get("slippage_fraction", 0.0),
        "market_data_csv": "session_date,close\ncorrupted,0\n",
        "market_data_metadata": {**payload["market_data_metadata"], "provider": "changed-after-review"},
        "expected_sessions": [],
        "preview_token": stale_preview["preview_token"],
    })
    assert stale_confirmation.status_code == 200, stale_confirmation.text
    stale_run_dir = tmp_path / "runs" / stale_confirmation.json()["run_id"]
    assert (stale_run_dir / "artifacts" / "market_data.csv").read_text() == raw_csv

    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "python-worker:latest")
    invalid_image_preview = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert invalid_image_preview.status_code == 200
    assert invalid_image_preview.json()["supported"] is False
    assert "pinned" in invalid_image_preview.json()["reason"]


def test_python_upload_strategy_failure_is_actionable_and_persisted_without_worker_text(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "a" * 64)

    class FailingSandbox:
        def __init__(self, image):
            assert image == "sha256:" + "a" * 64

        def run_json(self, request):
            from src.execution.python_sandbox import SandboxStrategyFailure
            raise SandboxStrategyFailure("strategy_runtime_error", "NameError", 3)

    import src.execution.python_sandbox as sandbox_module
    monkeypatch.setattr(sandbox_module, "PythonSandbox", FailingSandbox)
    days = [date(2025, 2, day) for day in (3, 4, 5)]
    sessions = []
    lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
    for index, day in enumerate(days):
        opened = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30)
        closed = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21)
        sessions.append({"session_date": day.isoformat(), "open_at": opened.isoformat(), "close_at": closed.isoformat()})
        price = 10 + index
        lines.append(
            f"{day.isoformat()},{opened.isoformat()},{closed.isoformat()},"
            f"{(closed + timedelta(minutes=1)).isoformat()},{price},{price + 1},{price - 1},{price}.5,1000"
        )
    payload = {
        "strategy_id": "api_python_failure", "instrument_id": "AAPL",
        "strategy_source": "def on_event(event, state):\n    print('SECRET-MARKER')\n    raise NameError('SECRET-MARKER')\n",
        "market_data_csv": "\n".join(lines) + "\n",
        "market_data_metadata": {
            "provider": "fixture_vendor", "instrument_id": "AAPL", "currency": "USD",
            "timezone": "America/New_York", "calendar_id": "XNYS", "calendar_version": "4.13.2",
            "availability_model": "completed bar available one minute after close",
            "adjustment_policy": "fixture_unadjusted", "corporate_action_policy": "fixture_none",
            "fetched_at": "2025-02-06T00:00:00Z", "warmup_start": days[0].isoformat(),
            "warmup_end": days[0].isoformat(), "evaluation_start": days[1].isoformat(),
            "evaluation_end": days[-1].isoformat(), "download_parameters": {"fixture": True},
        },
        "expected_sessions": sessions, "initial_cash": 1000, "commission_rate": 0.001,
    }
    preview = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert preview.status_code == 200, preview.text
    response = client.post("/runs/backtests/python-upload", json={
        **payload, "preview_token": preview.json()["preview_token"],
    })

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail == "Bar 1: Strategy code raised an error (NameError) on source line 3"
    assert "SECRET-MARKER" not in response.text
    run_dir = _run_directories(tmp_path / "runs")[0]
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["error_code"] == "strategy_runtime_error"
    assert state["event_seq"] == 1
    assert state["source_line"] == 3
    assert "SECRET-MARKER" not in json.dumps(state)


def test_python_upload_api_fails_closed_without_worker_image(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("VIBE_PYTHON_WORKER_IMAGE", raising=False)
    payload = {
        "strategy_id": "no_worker", "instrument_id": "AAPL", "strategy_source": "def on_event(event, state): pass",
        "market_data_csv": "csv", "market_data_metadata": {},
        "expected_sessions": [{"session_date": "2025-02-03", "open_at": "2025-02-03T14:30:00Z",
                               "close_at": "2025-02-03T21:00:00Z"}],
    }
    preview = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["supported"] is False
    response = client.post("/runs/backtests/python-upload", json=payload)
    assert response.status_code == 503


def test_python_upload_api_resumes_from_committed_event_results(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "b" * 64)

    class CrashOnceSandbox:
        total_calls = 0
        observed_state = []

        def __init__(self, image):
            assert image == "sha256:" + "b" * 64

        def run_json(self, request):
            type(self).total_calls += 1
            state = request["state"]
            type(self).observed_state.append(state.get("seen", 0))
            proposals = []
            if request["event"]["bar"]["session_date"] == days[1].isoformat():
                proposals = [{"side": "BUY", "integer_qty": 1, "reason": "recovery integration"}]
            return type("Result", (), {"response": {
                "protocol_version": request["protocol_version"],
                "request_id": request["request_id"],
                "event_id": request["event_id"],
                "state": {"seen": state.get("seen", 0) + 1},
                "order_proposals": proposals,
            }})()

    import src.execution.python_sandbox as sandbox_module
    monkeypatch.setattr(sandbox_module, "PythonSandbox", CrashOnceSandbox)
    from src.execution.event_worker import PythonBacktestEventCoordinator

    original_process_event = PythonBacktestEventCoordinator.process_event
    crash_state = {"raised": False}

    def crash_after_journal_commit(self, seq, *, feed_available_at):
        result = original_process_event(self, seq, feed_available_at=feed_available_at)
        if seq == 1 and not crash_state["raised"]:
            crash_state["raised"] = True
            raise RuntimeError("simulated host crash after journal commit before native order submission")
        return result

    monkeypatch.setattr(PythonBacktestEventCoordinator, "process_event", crash_after_journal_commit)

    days = [date(2025, 2, day) for day in (3, 4, 5, 6)]
    sessions = []
    lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
    for index, day in enumerate(days):
        opened = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30)
        closed = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21)
        sessions.append({"session_date": day.isoformat(), "open_at": opened.isoformat(), "close_at": closed.isoformat()})
        price = 20 + index
        lines.append(
            f"{day.isoformat()},{opened.isoformat()},{closed.isoformat()},"
            f"{(closed + timedelta(minutes=1)).isoformat()},{price},{price + 1},{price - 1},{price}.5,1000"
        )
    payload = {
        "strategy_id": "api_python_recovery",
        "instrument_id": "AAPL",
        "strategy_source": "def on_event(event, state):\n    return {'state': {'seen': state.get('seen', 0) + 1}, 'order_proposals': []}\n",
        "market_data_csv": "\n".join(lines) + "\n",
        "market_data_metadata": {
            "provider": "fixture_vendor", "instrument_id": "AAPL", "currency": "USD",
            "timezone": "America/New_York", "calendar_id": "XNYS", "calendar_version": "4.13.2",
            "availability_model": "completed bar available one minute after close",
            "adjustment_policy": "fixture_unadjusted", "corporate_action_policy": "fixture_none",
            "fetched_at": "2025-02-07T00:00:00Z", "warmup_start": days[0].isoformat(),
            "warmup_end": days[0].isoformat(), "evaluation_start": days[1].isoformat(),
            "evaluation_end": days[-1].isoformat(), "download_parameters": {"fixture": True},
        },
        "expected_sessions": sessions,
        "initial_cash": 1000,
        "commission_rate": 0.001,
    }

    failed = client.post("/runs/backtests/python-upload", json=_preview_python_upload(client, payload))
    assert failed.status_code == 502, failed.text
    run_id = _run_directories(tmp_path / "runs")[0].name
    run_dir = tmp_path / "runs" / run_id
    assert json.loads((run_dir / "state.json").read_text())["status"] == "failed"
    from src.bt_runtime.backtest_event_journal import SQLiteBacktestEventJournal

    journal = SQLiteBacktestEventJournal(run_dir / "artifacts" / "backtest_events.sqlite3")
    assert journal.get_status(run_id)["next_seq"] == 2

    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "a" * 64)
    wrong_worker_resume = client.post(f"/runs/{run_id}/resume")
    assert wrong_worker_resume.status_code == 409
    assert json.loads((run_dir / "state.json").read_text())["status"] == "failed"
    assert journal.get_status(run_id)["next_seq"] == 2
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "b" * 64)

    resumed = client.post(f"/runs/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    report = resumed.json()["execution_report"]
    assert report["execution_integrity"]["event_journal_state"] == "COMPLETE"
    assert report["execution_integrity"]["reused_committed_results"] == 1
    assert report["execution_integrity"]["newly_computed_results"] == len(days) - 1
    assert CrashOnceSandbox.total_calls == len(days)
    assert len(report["orders"]) == 1
    assert len(report["fills"]) == 1
    assert report["final_position"]["qty"] == 1
    assert json.loads((run_dir / "state.json").read_text())["status"] == "success"

    uninterrupted = client.post("/runs/backtests/python-upload", json=_preview_python_upload(client, payload))
    assert uninterrupted.status_code == 200, uninterrupted.text
    expected = uninterrupted.json()["execution_report"]
    for field in ("orders", "fills", "equity_curve", "closed_trades", "final_cash", "final_value", "final_position", "decision_log"):
        assert report[field] == expected[field]
    assert CrashOnceSandbox.observed_state == [0, 1, 2, 3, 0, 1, 2, 3]
