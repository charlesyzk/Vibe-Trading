import json
import sqlite3
from datetime import datetime, timezone

import backtrader
import pytest

from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
from src.execution.paper_integration_test_config import read_paper_integration_test_config
from src.execution.paper_strategy_deployment import (
    commit_paper_strategy_selection,
    paper_execution_ledger_is_empty,
    prepare_paper_strategy_selection,
    rollback_paper_strategy_selection,
)
from src.execution.versions import StrategyVersionStore
from tests.execution.test_paper_integration_test_config import _payload
from tests.execution.test_paper_strategy_candidates import _report, _write_run


def _candidate_root(root):
    version_store = StrategyVersionStore(root / "strategy-versions")
    spec, source = build_paper_integration_test_spec(datetime(2026, 10, 30, tzinfo=timezone.utc).date())
    version = version_store.create_version(
        spec,
        upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    _write_run(root, version)
    payload = _payload()
    payload["strategy_version_id"] = version.version_id
    config_path = root / "paper-integration-test.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return version, config_path


def test_selection_pins_verified_version_and_audits_operator_without_copying_account_data(tmp_path):
    version, config_path = _candidate_root(tmp_path)

    receipt = prepare_paper_strategy_selection(
        version.version_id,
        operator_subject="partner-dev",
        runtime_root=tmp_path,
        now=datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc),
    )
    assert read_paper_integration_test_config(config_path).strategy_version_id == version.version_id
    assert read_paper_integration_test_config(config_path).backtest_report_sha256 == receipt["report_sha256"]
    assert receipt["report_sha256"]
    commit_paper_strategy_selection(receipt)

    audit = (tmp_path / "paper-integration-test" / "deployments" / "selection-audit.jsonl").read_text()
    assert '"operator_subject":"partner-dev"' in audit
    assert version.version_id in audit
    assert "DU1234567" not in audit
    assert "previous_config_bytes" not in audit


def test_selection_can_roll_back_config_and_attestation_artifacts(tmp_path):
    version, config_path = _candidate_root(tmp_path)
    original = config_path.read_bytes()

    receipt = prepare_paper_strategy_selection(version.version_id, operator_subject="partner-dev", runtime_root=tmp_path)
    assert config_path.read_bytes() != original
    rollback_paper_strategy_selection(receipt)

    assert config_path.read_bytes() == original
    assert not list((tmp_path / "paper-integration-test" / "backtests").glob("*.json"))


def test_selection_binds_exact_requested_run_and_rejects_unknown_run(tmp_path):
    version, _ = _candidate_root(tmp_path)
    _write_run(tmp_path, version, "bt_another")
    receipt = prepare_paper_strategy_selection(
        version.version_id, run_id="bt_another", operator_subject="partner-dev", runtime_root=tmp_path,
    )
    assert receipt["run_id"] == "bt_another"
    rollback_paper_strategy_selection(receipt)
    with pytest.raises(ValueError, match="no verified Backtrader report"):
        prepare_paper_strategy_selection(
            version.version_id, run_id="bt_missing", operator_subject="partner-dev", runtime_root=tmp_path,
        )


def test_selection_pins_report_even_when_a_newer_report_exists(tmp_path):
    from src.execution.paper_backtest_attestation import (
        verify_paper_backtest_attestation, write_paper_backtest_attestation,
    )

    version, config_path = _candidate_root(tmp_path)
    directory = tmp_path / "paper-integration-test" / "backtests"
    directory.mkdir(parents=True)
    newer = directory / f"20990101T000000Z-{version.version_id}.json"
    different_report = _report(version)
    different_report["fixture_note"] = "different report"
    newer.write_text(json.dumps(different_report))
    write_paper_backtest_attestation(newer, version_id=version.version_id)
    receipt = prepare_paper_strategy_selection(
        version.version_id, run_id="bt_20261031_120000_abcdef123456",
        operator_subject="partner-dev", runtime_root=tmp_path,
    )
    config = read_paper_integration_test_config(config_path)
    pinned = verify_paper_backtest_attestation(
        directory, version=version, report_sha256=config.backtest_report_sha256,
    )
    assert pinned.report_sha256 == receipt["report_sha256"]
    assert pinned.report_path != newer
    assert verify_paper_backtest_attestation(directory, version=version).report_path == newer
    pinned.report_path.unlink()
    with pytest.raises(ValueError, match="no intact"):
        verify_paper_backtest_attestation(directory, version=version, report_sha256=config.backtest_report_sha256)


def test_selection_refuses_a_nonempty_order_ledger(tmp_path):
    runtime = tmp_path / "paper-integration-test"
    runtime.mkdir()
    database = runtime / "execution.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE order_intent (id TEXT)")
        connection.execute("INSERT INTO order_intent VALUES ('already-used')")

    assert paper_execution_ledger_is_empty(tmp_path) is False


def test_selection_refuses_backtest_outside_fixed_order_shape(tmp_path):
    version, config_path = _candidate_root(tmp_path)
    run_report = tmp_path / "runs" / "bt_20261031_120000_abcdef123456" / "artifacts" / "execution_report.json"
    report = json.loads(run_report.read_text())
    report["orders"][0]["limit_price"] = 399.0
    run_report.write_text(json.dumps(report))
    from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar
    write_report_integrity_sidecar(run_report)

    try:
        prepare_paper_strategy_selection(version.version_id, operator_subject="partner-dev", runtime_root=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("incompatible report was selected")
    assert read_paper_integration_test_config(config_path).strategy_version_id == version.version_id
