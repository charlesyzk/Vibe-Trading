import json
from datetime import date

import backtrader
import pytest

from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_backtest_attestation import write_paper_backtest_attestation
from src.execution.paper_strategy_candidates import list_paper_strategy_candidates
from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
from src.execution.versions import StrategyVersionStore
from tests.execution.test_paper_integration_test_config import _payload


def _version(root):
    store = StrategyVersionStore(root / "strategy-versions")
    spec, source = build_paper_integration_test_spec(date(2026, 10, 30))
    return store.create_version(
        spec,
        upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )


def _report(version):
    digest = "a" * 64
    import_attestation = {
        "schema_version": "ibkr-paper-import-attestation/1",
        "profile_id": "ibkr-paper-local",
        "environment": "paper",
        "symbol": "AAPL",
        "instrument_id": "AAPL",
        "con_id": 265598,
        "retrieved_at": "2026-10-30T21:01:00Z",
        "csv_sha256": "b" * 64,
        "calendar_schedule_sha256": "c" * 64,
        "snapshot_sha256": "d" * 64,
    }
    return {
        "execution_backend": "backtrader",
        "execution_mode": "backtest",
        "strategy_version_id": version.version_id,
        "strategy_version_hash": version.version_id,
        "strategy_id": version.strategy_id,
        "source_kind": version.source_kind.value,
        "instrument_id": "AAPL",
        "engine": f"Backtrader {backtrader.__version__} / BackBroker",
        "data_manifest_hash": digest,
        "data_manifest": {
            "provider": "IBKR PAPER Gateway host-observed import",
            "instrument_id": "AAPL",
            "file_sha256": "b" * 64,
            "quality_report": {"calendar_schedule_sha256": "c" * 64},
            "download_parameters": {
                "application_ibkr_paper_import_attestation": import_attestation,
                "qualified_contract": {
                    "symbol": "AAPL", "sec_type": "STK", "currency": "USD",
                    "con_id": 265598, "exchange": "SMART", "primary_exchange": "NASDAQ",
                },
            },
            "evaluation_start": "2026-10-30",
            "evaluation_end": "2026-10-30",
        },
        "execution_integrity": {"status": "verified", "data_manifest_hash": digest},
        "provenance": {"strategy_version_hash": version.version_id},
        "evaluation_bar_count": 1,
        "orders": [{
            "decision_date": "2026-10-30",
            "side": "BUY",
            "requested_qty": 1,
            "order_type": "limit",
            "limit_price": 400.0,
            "filled_qty": 0,
            "status": "OPEN_AT_DATA_END",
        }],
        "fills": [],
    }


def _write_run(root, version, run_id="bt_20261031_120000_abcdef123456"):
    run = root / "runs" / run_id
    artifacts = run / "artifacts"
    artifacts.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
    (run / "config.json").write_text(json.dumps({
        "execution_backend": "backtrader",
        "ibkr_paper_import_attestation": _report(version)["data_manifest"]["download_parameters"][
            "application_ibkr_paper_import_attestation"
        ],
    }), encoding="utf-8")
    path = artifacts / "execution_report.json"
    path.write_text(json.dumps(_report(version)), encoding="utf-8")
    write_report_integrity_sidecar(path)
    return path


def test_catalog_lists_verified_frozen_backtest_and_explains_paper_block(tmp_path):
    version = _version(tmp_path)
    _write_run(tmp_path, version)

    result = list_paper_strategy_candidates(runtime_root=tmp_path)

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["strategy_version_id"] == version.version_id
    assert item["backtest_status"] == "verified"
    assert item["frozen_quantity"] == 1
    assert item["frozen_limit_price"] == "400.00"
    assert item["paper_deployable"] is True
    assert item["currently_deployed"] is False


def test_catalog_only_marks_exact_pinned_version_deployable_with_one_shot_attestation(tmp_path):
    version = _version(tmp_path)
    _write_run(tmp_path, version)
    payload = _payload()
    payload["strategy_version_id"] = version.version_id
    (tmp_path / "paper-integration-test.json").write_text(json.dumps(payload), encoding="utf-8")
    report_dir = tmp_path / "paper-integration-test" / "backtests"
    report_dir.mkdir(parents=True)
    report_path = report_dir / f"20261031T120000Z-{version.version_id}.json"
    report_path.write_text(json.dumps(_report(version)), encoding="utf-8")
    write_paper_backtest_attestation(report_path, version_id=version.version_id)

    result = list_paper_strategy_candidates(runtime_root=tmp_path)

    assert len(result["items"]) == 2
    selected = next(item for item in result["items"] if item["currently_deployed"])
    assert selected["paper_deployable"] is True
    assert selected["run_id"] == "paper-integration-test"
    assert any(item["run_id"] == "bt_20261031_120000_abcdef123456" for item in result["items"])


def test_catalog_preserves_multiple_runs_of_same_version(tmp_path):
    version = _version(tmp_path)
    _write_run(tmp_path, version, "bt_first")
    _write_run(tmp_path, version, "bt_second")
    items = list_paper_strategy_candidates(runtime_root=tmp_path)["items"]
    assert {item["run_id"] for item in items} == {"bt_first", "bt_second"}


def test_catalog_rejects_caller_spoofed_ibkr_paper_provider_without_server_attestation(tmp_path):
    version = _version(tmp_path)
    path = _write_run(tmp_path, version)
    run_config = path.parent.parent / "config.json"
    run_config.write_text(json.dumps({"execution_backend": "backtrader"}), encoding="utf-8")

    result = list_paper_strategy_candidates(runtime_root=tmp_path)

    assert len(result["items"]) == 1
    assert result["items"][0]["paper_deployable"] is False
    assert "backtest_data_lacks_server_verified_ibkr_paper_import" in result["items"][0]["paper_block_reasons"]


@pytest.mark.parametrize("field,value", [
    ("con_id", 1234), ("symbol", "MSFT"), ("sec_type", "OPT"), ("currency", "EUR"),
])
def test_catalog_rejects_contract_identity_mismatch_even_with_intact_report(tmp_path, field, value):
    version = _version(tmp_path)
    path = _write_run(tmp_path, version)
    report = json.loads(path.read_text())
    report["data_manifest"]["download_parameters"]["qualified_contract"][field] = value
    path.write_text(json.dumps(report), encoding="utf-8")
    write_report_integrity_sidecar(path)

    item = list_paper_strategy_candidates(runtime_root=tmp_path)["items"][0]
    assert item["paper_deployable"] is False
    assert "backtest_data_lacks_server_verified_ibkr_paper_import" in item["paper_block_reasons"]


def test_catalog_skips_failed_run_and_report_without_integrity_sidecar(tmp_path):
    version = _version(tmp_path)
    path = _write_run(tmp_path, version)
    (path.parent.parent / "state.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")

    result = list_paper_strategy_candidates(runtime_root=tmp_path)
    assert result["items"] == []

    (path.parent.parent / "state.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
    path.with_name("execution_report.integrity.json").unlink()
    assert list_paper_strategy_candidates(runtime_root=tmp_path)["items"] == []


def test_catalog_skips_symlinked_artifact_directory(tmp_path):
    version = _version(tmp_path)
    path = _write_run(tmp_path, version)
    run_dir = path.parent.parent
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    path.unlink()
    path.with_name("execution_report.integrity.json").unlink()
    (run_dir / "artifacts").rmdir()
    (run_dir / "artifacts").symlink_to(outside, target_is_directory=True)

    assert list_paper_strategy_candidates(runtime_root=tmp_path)["items"] == []
