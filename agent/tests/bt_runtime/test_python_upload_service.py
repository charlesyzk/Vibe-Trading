from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import backtrader as bt

from src.bt_runtime.market_data import CSV_COLUMNS, MarketDataMetadata, MarketSession, import_completed_daily_csv
from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
from src.execution.contracts import StrategySpec
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.versions import StrategyVersionStore


class NoOrderSandbox:
    def run_json(self, request):
        return type("Result", (), {"response": {
            "protocol_version": request["protocol_version"], "request_id": request["request_id"],
            "event_id": request["event_id"], "state": request["state"], "order_proposals": [],
        }})()


def test_service_persists_exact_imported_data_version_manifest_and_backtrader_report(tmp_path):
    days = [date(2025, 2, day) for day in (3, 4, 5, 6, 7)]
    sessions = [MarketSession(
        day,
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
    ) for day in days]
    rows = [CSV_COLUMNS]
    for index, session in enumerate(sessions):
        open_value = 10 + index
        close = open_value + 0.1
        rows.append((
            session.session_date.isoformat(), session.open_at.isoformat(), session.close_at.isoformat(),
            (session.close_at + timedelta(minutes=1)).isoformat(), str(open_value), str(open_value + 1),
            str(open_value - 1), str(close), "1000",
        ))
    raw_csv = ("\n".join(",".join(map(str, row)) for row in rows) + "\n").encode()
    metadata = MarketDataMetadata(
        provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
        calendar_id="fixture_calendar", calendar_version="2025.1",
        availability_model="completed bar available one minute after close",
        adjustment_policy="unadjusted_fixture", corporate_action_policy="none_fixture",
        fetched_at=datetime(2025, 2, 10, tzinfo=timezone.utc), warmup_start=days[0], warmup_end=days[0],
        evaluation_start=days[1], evaluation_end=days[-1], download_parameters={"fixture": True},
    )
    imported = import_completed_daily_csv(raw_csv, metadata, sessions)
    spec = StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "python_upload_service_fixture",
        "source_kind": "python_upload", "source_artifact_id": "upload-1", "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
        "parameters": {}, "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
    })
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        spec, engine_version=f"backtrader/{bt.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
        upload_source=b"def on_event(event, state):\n    return {\"state\": state, \"order_proposals\": []}\n",
    )
    run_dir = tmp_path / "run"

    report = run_python_upload_service(
        version_id=version.version_id, versions=versions, market_data=imported, raw_csv=raw_csv,
        sandbox=NoOrderSandbox(), run_dir=run_dir, initial_cash=1000, slippage_fraction=0.005,
    )

    artifacts = run_dir / "artifacts"
    assert (artifacts / "market_data.csv").read_bytes() == raw_csv
    assert hashlib.sha256((artifacts / "market_data.csv").read_bytes()).hexdigest() == imported.manifest.file_sha256
    assert json.loads((artifacts / "data_manifest.json").read_text()) == imported.manifest.model_dump(mode="json")
    saved_version = json.loads((artifacts / "strategy_version.json").read_text())
    assert saved_version["version_id"] == version.version_id
    saved_report = json.loads((artifacts / "execution_report.json").read_text())
    assert saved_report == json.loads(json.dumps(report, allow_nan=False))
    from src.bt_runtime.report_artifact_integrity import verify_report_integrity

    assert verify_report_integrity(artifacts / "execution_report.json") == (True, None)
    assert saved_report["execution_backend"] == "backtrader"
    assert saved_report["strategy_version_hash"] == version.version_hash
    assert saved_report["data_manifest"]["file_sha256"] == imported.raw_sha256
    saved_run_config = json.loads((artifacts / "backtest_run_config.json").read_text())
    assert saved_run_config["slippage_fraction"] == 0.005
    assert saved_report["provenance"]["cost"]["slippage_fraction"] == "0.005"
