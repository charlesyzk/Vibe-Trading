import json
from datetime import date

import backtrader
import pytest

from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_backtest_attestation import (
    PaperBacktestAttestationError,
    verify_paper_backtest_attestation,
    write_paper_backtest_attestation,
)
from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
from src.execution.versions import StrategyVersionStore


def _frozen_version(tmp_path, signal_day=date(2026, 10, 30)):
    store = StrategyVersionStore(tmp_path / "versions")
    spec, source = build_paper_integration_test_spec(signal_day)
    record = store.create_version(
        spec,
        upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    return record


def _report(version):
    digest = "a" * 64
    return {
        "strategy_version_hash": version.version_id,
        "strategy_id": version.strategy_id,
        "source_kind": "python_upload",
        "engine": f"Backtrader {backtrader.__version__} / BackBroker",
        "instrument_id": "AAPL",
        "data_manifest_hash": digest,
        "data_manifest": {"evaluation_start": "2026-10-30", "evaluation_end": "2026-10-30"},
        "execution_integrity": {"status": "verified", "data_manifest_hash": digest},
        "evaluation_bar_count": 1,
        "provenance": {"strategy_version_hash": version.version_id},
    }


def test_attestation_binds_report_to_immutable_version_and_verified_backtest(tmp_path):
    version = _frozen_version(tmp_path)
    report_dir = tmp_path / "backtests"
    report_dir.mkdir()
    path = report_dir / f"20261031T120000Z-{version.version_id}.json"
    path.write_text(json.dumps(_report(version)), encoding="utf-8")

    sidecar = write_paper_backtest_attestation(path, version_id=version.version_id)
    result = verify_paper_backtest_attestation(report_dir, version=version)

    assert sidecar["strategy_version_id"] == version.version_id
    assert result.version_id == version.version_id
    assert result.report_sha256 == sidecar["sha256"]
    assert result.data_manifest_hash == "a" * 64
    assert result.evaluation_start == "2026-10-30"


def test_attestation_rejects_report_replacement(tmp_path):
    version = _frozen_version(tmp_path)
    report_dir = tmp_path / "backtests"
    report_dir.mkdir()
    path = report_dir / f"20261031T120000Z-{version.version_id}.json"
    path.write_text(json.dumps(_report(version)), encoding="utf-8")
    write_paper_backtest_attestation(path, version_id=version.version_id)
    path.write_text(json.dumps({**_report(version), "fills": [{"qty": 1000}]}), encoding="utf-8")

    with pytest.raises(PaperBacktestAttestationError):
        verify_paper_backtest_attestation(report_dir, version=version)


def test_attestation_rejects_report_from_another_version(tmp_path):
    version = _frozen_version(tmp_path)
    other = _frozen_version(tmp_path / "other", date(2026, 10, 29))
    report_dir = tmp_path / "backtests"
    report_dir.mkdir()
    path = report_dir / f"20261031T120000Z-{other.version_id}.json"
    path.write_text(json.dumps(_report(other)), encoding="utf-8")
    write_paper_backtest_attestation(path, version_id=other.version_id)

    with pytest.raises(PaperBacktestAttestationError):
        verify_paper_backtest_attestation(report_dir, version=version)


def test_attestation_rejects_unverified_execution_integrity(tmp_path):
    version = _frozen_version(tmp_path)
    report_dir = tmp_path / "backtests"
    report_dir.mkdir()
    report = _report(version)
    report["execution_integrity"]["status"] = "unverified"
    path = report_dir / f"20261031T120000Z-{version.version_id}.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(PaperBacktestAttestationError):
        write_paper_backtest_attestation(path, version_id=version.version_id)


def test_attestation_supports_other_instruments_but_rejects_version_mismatch(tmp_path):
    spec, source = build_paper_integration_test_spec(date(2026, 10, 30))
    data = spec.model_dump(mode="json")
    data["instrument_ids"] = ["MSFT"]
    data["parameters"]["quantity"] = 5
    version = StrategyVersionStore(tmp_path / "versions").create_version(
        type(spec).model_validate(data), upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    report_dir = tmp_path / "backtests"
    report_dir.mkdir()
    path = report_dir / f"20261031T120000Z-{version.version_id}.json"
    report = _report(version)
    report["instrument_id"] = "MSFT"
    path.write_text(json.dumps(report), encoding="utf-8")
    write_paper_backtest_attestation(path, version_id=version.version_id)
    assert verify_paper_backtest_attestation(report_dir, version=version).version_id == version.version_id

    report["instrument_id"] = "AAPL"
    path.write_text(json.dumps(report), encoding="utf-8")
    write_paper_backtest_attestation(path, version_id=version.version_id)
    with pytest.raises(PaperBacktestAttestationError):
        verify_paper_backtest_attestation(report_dir, version=version)
