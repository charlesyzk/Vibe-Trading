from datetime import datetime, timezone
from decimal import Decimal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.execution.contracts import Environment
from src.execution.local_paper_draft_factory import build_local_paper_draft_composition
import src.execution.local_paper_draft_factory as local_factory
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_draft import save_paper_order_draft
from src.execution.paper_order_preparation import PaperOrderSetup
from src.execution.risk import ProposedOrder, evaluate_order_risk
from src.session.models import AuthMethod, Principal
from src.trading.connectors.ibkr.local import IBKRLocalConfig
from tests.execution.test_paper_strategy_candidates import _version, _write_run
from tests.execution.test_outbox_dispatch import _bar
from tests.execution.test_risk import make_facts


@pytest.mark.parametrize("auth_method", [AuthMethod.LOCAL_OPERATOR_TOKEN, AuthMethod.LOCAL_DESKTOP_PROFILE])
def test_local_factory_wires_saved_draft_without_connecting(tmp_path, monkeypatch, auth_method):
    version = _version(tmp_path)
    _write_run(tmp_path, version)
    setup = PaperOrderSetup.model_validate({"valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2})
    order = ProposedOrder(instrument_id="AAPL", side="BUY", integer_qty=1,
                          order_type="limit", time_in_force="DAY", limit_price=Decimal("400"))
    save_paper_order_draft(runtime_root=tmp_path, account_ref="DU123", run_id="bt_20261031_120000_abcdef123456",
                           version_id=version.version_id, order=order, setup=setup)
    monkeypatch.setattr(local_factory, "get_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(local_factory, "load_config", lambda: IBKRLocalConfig(
        profile="paper", host="127.0.0.1", port=4002, account="DU123", readonly=True))
    monkeypatch.setattr(local_factory, "_ibkr_runtime_paths", lambda **_: (sys.executable, tmp_path))
    monkeypatch.setattr(local_factory, "_setting", lambda key: "sha256:" + "a" * 64 if key == "VIBE_PYTHON_WORKER_IMAGE" else "")
    monkeypatch.setattr(local_factory, "build_paper_warmup", lambda **_: SimpleNamespace(checkpoint={"warmup": 2}, signal_bar=_bar()))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_factory, "datetime", Clock)
    scope = PaperRuntimeScope("IBKR", "DU123", Environment.PAPER)
    principal = Principal(subject="partner", auth_method=auth_method)
    composition = build_local_paper_draft_composition(scope, principal)
    assert composition.manager.status() is None
    assert composition.draft_runner.manager is composition.manager
    provider = composition.manager.runtime_factory.session_builder.dispatch_risk_facts
    assert composition.manager.runtime_factory.session_builder.initial_checkpoint == {"warmup": 2}
    assert provider.order_policy.order == order
    assert provider.risk_grant.max_order_notional == Decimal("500")
    assert provider.metrics_reader.previous_bar_reader.__self__.contract.symbol == "AAPL"
    assert Path(composition.manager._stopped_activity[1].path) == tmp_path / "paper-integration-test" / "execution.sqlite"
    assert provider.snapshot_reader._runtime is None
    authorize = composition.manager.control_authorizer
    assert authorize(principal, scope, "start")
    assert not authorize(principal, scope, "activate")
    assert authorize(principal, scope, "cancel")
    assert not authorize(Principal(subject="other", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN), scope, "cancel")
    assert not authorize(principal, PaperRuntimeScope("IBKR", "DU456", Environment.PAPER), "cancel")
    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_factory, "datetime", LaterClock)
    recovered = build_local_paper_draft_composition(scope, principal)
    recovered_grant = recovered.manager.runtime_factory.session_builder.dispatch_risk_facts.risk_grant
    assert recovered_grant.granted_at == provider.risk_grant.granted_at
    assert recovered_grant.valid_until == setup.valid_until
    assert recovered.manager.status() is None
    assert not recovered.manager.control_authorizer(principal, scope, "start")
    assert not recovered.manager.control_authorizer(principal, scope, "activate")
    assert recovered.manager.control_authorizer(principal, scope, "cancel")
    assert recovered.manager.control_authorizer(principal, scope, "stop")
    later = LaterClock.now(timezone.utc)
    facts = make_facts(account_ref="DU123", strategy_version_hash=version.version_id, now=later,
                       quote_as_of=later, account_as_of=later)
    assert "grant_expired_or_not_yet_valid" in evaluate_order_risk(recovered_grant, order, facts).reason_codes
    monkeypatch.setattr(local_factory, "load_config", lambda: IBKRLocalConfig(
        profile="live-readonly", host="127.0.0.1", port=4001, account="DU123", readonly=True))
    with pytest.raises(ValueError, match="PAPER account"):
        build_local_paper_draft_composition(scope, principal)
