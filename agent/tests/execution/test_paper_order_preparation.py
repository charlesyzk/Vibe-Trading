import json
from datetime import date, datetime, timezone
from decimal import Decimal

import backtrader
import pytest

from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
from src.execution.paper_order_preparation import PaperOrderSetup, prepare_verified_paper_order, suggest_paper_entry_window, next_session_dispatch_close
from src.execution.risk import ProposedOrder
from src.execution.versions import StrategyVersionStore
from tests.execution.test_paper_strategy_candidates import _report


@pytest.mark.parametrize(("now", "start", "end"), [
    ("2026-11-01T12:00:00+00:00", "2026-11-02T14:30:00+00:00", "2026-11-02T21:00:00+00:00"),
    ("2026-11-02T15:00:00+00:00", "2026-11-02T15:00:00+00:00", "2026-11-02T21:00:00+00:00"),
    ("2026-11-02T20:58:00+00:00", "2026-11-03T14:30:00+00:00", "2026-11-03T21:00:00+00:00"),
    ("2026-11-27T12:00:00+00:00", "2026-11-27T14:30:00+00:00", "2026-11-27T18:00:00+00:00"),
])
def test_suggested_window_respects_calendar_and_early_close(now, start, end):
    result = suggest_paper_entry_window(datetime.fromisoformat(now))
    assert tuple(value.isoformat() for value in result) == (start, end)


def test_dispatch_window_uses_next_session_and_rejects_stale_or_closed():
    signal = datetime(2026, 10, 30, 20, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    assert next_session_dispatch_close(signal, monday) == datetime(2026, 11, 2, 21, tzinfo=timezone.utc)
    for invalid in (datetime(2026, 11, 2, 21, tzinfo=timezone.utc),
                    datetime(2026, 11, 3, 15, tzinfo=timezone.utc),
                    datetime(2026, 11, 1, 15, tzinfo=timezone.utc)):
        with pytest.raises(ValueError):
            next_session_dispatch_close(signal, invalid)


def test_multi_session_setup_bounds_endpoints_and_duration():
    values = {"valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-06T21:00:00Z",
              "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
              "max_daily_loss": "100", "max_orders_per_session": 2}
    setup = PaperOrderSetup.model_validate(values)
    assert setup.valid_until.date() == date(2026, 11, 6)
    with pytest.raises(ValueError, match="endpoints"):
        PaperOrderSetup.model_validate({**values, "valid_until": "2026-11-07T21:00:00Z"})
    with pytest.raises(ValueError, match="90 calendar days"):
        PaperOrderSetup.model_validate({**values, "valid_until": "2027-03-01T21:00:00Z"})


@pytest.mark.parametrize("symbol,quantity,con_id", [("AAPL", 1, 265598), ("MSFT", 5, 272093)])
def test_prepares_exact_order_and_contract_from_verified_evidence(tmp_path, symbol, quantity, con_id):
    spec, source = build_paper_integration_test_spec(date(2026, 10, 30))
    payload = spec.model_dump(mode="json")
    payload["instrument_ids"] = [symbol]
    payload["parameters"]["quantity"] = quantity
    version = StrategyVersionStore(tmp_path / "strategy-versions").create_version(
        type(spec).model_validate(payload), upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}", compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    report = _report(version)
    report["instrument_id"] = symbol
    report["data_manifest"]["instrument_id"] = symbol
    data = report["data_manifest"]["download_parameters"]
    proof = data["application_ibkr_paper_import_attestation"]
    proof.update(symbol=symbol, instrument_id=symbol, con_id=con_id)
    data["qualified_contract"].update(symbol=symbol, con_id=con_id)
    run = tmp_path / "runs" / "run-1"
    artifacts = run / "artifacts"
    artifacts.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"status": "success"}))
    (run / "config.json").write_text(json.dumps({"execution_backend": "backtrader", "ibkr_paper_import_attestation": proof}))
    path = artifacts / "execution_report.json"
    path.write_text(json.dumps(report))
    write_report_integrity_sidecar(path)
    order = ProposedOrder(instrument_id=symbol, side="BUY", integer_qty=quantity, order_type="limit",
                          time_in_force="DAY", limit_price=Decimal("400"))
    def prepare(selected=order):
        return prepare_verified_paper_order(runtime_root=tmp_path, run_id="run-1", version_id=version.version_id,
                                            account_ref="DU123", order=selected)
    result = prepare()
    assert result.policy.order == order
    assert result.contract.symbol == symbol and result.contract.con_id == con_id
    assert len(result.report_sha256) == 64
    setup = PaperOrderSetup.model_validate({
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "2500", "max_gross_exposure": "3000", "strategy_budget": "3000",
        "max_daily_loss": "200", "max_orders_per_session": 3,
    })
    grant = result.build_risk_grant(setup, grant_id="grant", user_action_id="operator-action",
                                    granted_at=datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))
    assert grant.account_ref == "DU123" and grant.environment.value == "paper"
    assert grant.strategy_version_hash == version.version_id
    assert grant.allowed_instruments == (symbol,) and grant.allowed_order_types == ("limit",)
    assert grant.max_order_notional == Decimal("2500") and grant.max_orders_per_session == 3
    assert grant.valid_from == setup.valid_from and grant.valid_until == setup.valid_until
    assert grant.funds_check == "account_cash"
    # Exercise both proposal and pre-send providers with the same prepared
    # account/version/order grant; all market and broker inputs are fixtures.
    from types import SimpleNamespace
    from src.execution.ibkr_paper_risk_facts import IbkrPaperRiskFactsProvider, StrategyRiskMetrics
    from src.execution.outbox_dispatch import OutboxProposal
    from tests.execution.test_ibkr_paper_risk_facts import quote
    from tests.execution.test_outbox_dispatch import _bar

    now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    snapshot = {"capture_complete": True, "reconciled": True, "account_ref": "DU123",
                "environment": "paper", "facts_sha256": "b" * 64, "completed_at_utc": now.isoformat(),
                "positions": [], "open_orders": [], "account_summary": [
                    {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
                    {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
                    {"tag": "AvailableFunds", "currency": "USD", "value": "5000"},
                ]}
    response = quote(observed=now)
    response["qualified_contract"].update(symbol=symbol, con_id=con_id)
    metrics = StrategyRiskMetrics(0, 0, Decimal("0"), Decimal("0"), 0, Decimal("0.05"), Decimal("1"))
    provider = IbkrPaperRiskFactsProvider(
        account_ref="DU123", strategy_version_hash=version.version_id, risk_grant=grant,
        contracts={symbol: result.contract}, fx_contract_ids={}, snapshot_reader=lambda: snapshot,
        quote_reader=lambda _contract: response, fx_quote_reader=lambda _currency: {},
        metrics_reader=lambda *_: metrics, order_policy=result.policy, now=lambda: now,
    )
    proposal = OutboxProposal(
        run_id="run", event_id="event", seq=1, proposal_index=0,
        proposal={"side": "BUY", "integer_qty": quantity, "order_type": "limit",
                  "time_in_force": "DAY", "limit_price": "400"},
        identity={"account_ref": "DU123", "environment": "paper", "strategy_version_hash": version.version_id,
                  "deployment_id": "deployment"}, bar=_bar().model_copy(update={"instrument_id": symbol, "end_at": datetime(2026, 10, 30, 20, tzinfo=timezone.utc)}),
    )
    first = provider.proposal_inputs(proposal)
    intent = SimpleNamespace(broker="IBKR", account_ref="DU123", environment=grant.environment,
                             strategy_version_hash=version.version_id, instrument_id=symbol, side="BUY",
                             integer_qty=quantity, order_type="limit", time_in_force="DAY",
                             limit_price=Decimal("400"), deployment_id="deployment", bar_end=proposal.bar.end_at)
    second = provider(intent, excluding_intent_id="fixture")
    assert first.grant == second.grant == grant
    assert first.facts.available_cash == second.facts.available_cash == Decimal("5000")
    with pytest.raises(ValueError, match="attributable"):
        result.build_risk_grant(setup, grant_id="", user_action_id="action",
                                granted_at=datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="maximum order notional"):
        result.build_risk_grant(setup.model_copy(update={"max_order_notional": Decimal("1")}),
                                grant_id="grant", user_action_id="action",
                                granted_at=datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="frozen strategy"):
        prepare(order.model_copy(update={"integer_qty": quantity + 1}))
    data["qualified_contract"]["con_id"] = con_id + 1
    path.write_text(json.dumps(report))
    write_report_integrity_sidecar(path)
    with pytest.raises(ValueError, match="market data and contract"):
        prepare()
