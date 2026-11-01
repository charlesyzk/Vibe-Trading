from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.execution.contracts import Environment
from src.execution.order_ledger import ExecutionControlState
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_integration_test_composition import PaperIntegrationTestRunner
from src.session.models import AuthMethod, Principal
from tests.execution.test_paper_integration_test_config import _payload


class _MondayWindowClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


def _activity():
    return {
        "orders": [{"intent_id": "paper-order", "state": "FILLED"}],
        "recovery": {"active_orders": 0, "unresolved_sends": 0},
        "positions": {"source": "ibkr_reconciled_snapshot", "positions": []},
    }


def test_runner_uses_one_authorized_signal_then_pauses_and_reconciles(monkeypatch, tmp_path: Path):
    import src.execution.paper_integration_test_composition as composition_module

    payload = _payload()
    payload["enabled"] = True
    version_id = "a" * 64
    payload["strategy_version_id"] = version_id
    config_path = tmp_path / "paper-integration-test.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    config_path.chmod(0o600)
    monkeypatch.setattr(composition_module, "datetime", _MondayWindowClock)
    monkeypatch.setattr(composition_module, "get_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(
        composition_module,
        "StrategyVersionStore",
        lambda _root: SimpleNamespace(get_version=lambda _version_id: SimpleNamespace(version_id=version_id)),
    )
    monkeypatch.setattr(
        composition_module,
        "_require_fixed_order_backtest",
        lambda *_args, **_kwargs: SimpleNamespace(
            report_sha256="b" * 64,
            data_manifest_hash="c" * 64,
            evaluation_start="2026-10-30",
            evaluation_end="2026-10-30",
        ),
    )

    submitted = []
    pauses = []
    expected_activity = _activity()
    manager = SimpleNamespace(
        start_activate_and_push_completed_bar=lambda *args, **kwargs: submitted.append((args, kwargs)),
        activity_snapshot=lambda **_: _activity(),
        pause_and_refresh_account_snapshot=lambda scope: pauses.append(scope) or expected_activity,
        status=lambda: SimpleNamespace(
            phase="READY", control_state=ExecutionControlState.PAUSED,
        ),
    )
    scope = PaperRuntimeScope("IBKR", "DUR123456", Environment.PAPER)
    bar = SimpleNamespace(session_date=__import__("datetime").date(2026, 10, 30))
    runner = PaperIntegrationTestRunner(
        manager=manager,
        scope=scope,
        config_path=config_path,
        source=lambda: [bar],
        signal_session_date=bar.session_date,
        version_id=version_id,
    )
    principal = Principal("partner-alice", AuthMethod.FEDERATED_IDENTITY)

    status = runner.status_snapshot()
    result = runner(principal)

    assert status["window_open"] is True
    assert status["limit_price"] == "400.00"
    assert status["backtest_attestation"]["status"] == "verified"
    assert status["backtest_attestation"]["suggested_warmup_date"] == "2026-10-29"
    assert "account_ref" not in status
    assert submitted == [((scope, principal), {
        "expected_strategy_version_id": version_id,
        "bar": bar,
    })]
    assert pauses == [scope]
    assert result["dispatch_gate_open"] is False
    assert result["control_state"] == "PAUSED"
    assert result["post_order_reconciliation"] == "verified"
    assert result["activity"] is expected_activity
