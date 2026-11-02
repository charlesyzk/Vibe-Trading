from datetime import datetime, timezone
from decimal import Decimal
import json

import backtrader
import pytest

from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar
from src.execution.contracts import Environment
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.event_journal import SQLiteEventJournal
from src.execution.python_upload_paper_session_builder import PythonUploadPaperSessionBuildError
from src.execution.dispatch_service import PaperOrderDispatchService
from src.execution.ibkr_paper_risk_facts import IbkrPaperRiskFactsError, StrategyRiskMetrics
from src.execution.ibkr_paper_risk_facts import IbkrManagedRiskSnapshotSource
from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
from src.execution.paper_draft_composition import build_paper_draft_composition
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_draft import save_paper_order_draft
from src.execution.paper_order_preparation import PaperOrderSetup
from src.execution.order_ledger import ExecutionControlState, SQLiteOrderLedger
from src.execution.outbox_dispatch import OutboxProposal, RiskAwareProposalAuthorizer
from src.execution.python_sandbox import PythonSandbox
from src.execution.risk import ProposedOrder
from src.execution.versions import StrategyVersionStore
from tests.execution.test_paper_strategy_candidates import _version, _write_run
from tests.execution.test_python_upload_paper_composition_factory import _runtime_config, _runtime_dependencies
from tests.execution.test_ibkr_paper_risk_facts import quote
from tests.execution.test_outbox_dispatch import _bar
from src.trading.connectors.ibkr.local import IBKRLocalConfig


@pytest.mark.parametrize("symbol,quantity,con_id", [("AAPL", 1, 265598), ("MSFT", 2, 272093)])
def test_saved_draft_builds_unstarted_composition_with_exact_order_and_grant(tmp_path, symbol, quantity, con_id):
    if symbol == "AAPL":
        version = _version(tmp_path)
    else:
        spec, source = build_paper_integration_test_spec(datetime(2026, 10, 30).date())
        spec_data = spec.model_dump(mode="json")
        spec_data["instrument_ids"] = [symbol]
        spec_data["parameters"]["quantity"] = quantity
        version = StrategyVersionStore(tmp_path / "strategy-versions").create_version(
            type(spec).model_validate(spec_data), upload_source=source,
            engine_version=f"backtrader/{backtrader.__version__}", compiler_version=PYTHON_WORKER_COMPILER_VERSION,
        )
    report_path = _write_run(tmp_path, version)
    if symbol != "AAPL":
        report = json.loads(report_path.read_text())
        report["instrument_id"] = symbol
        report["data_manifest"]["instrument_id"] = symbol
        params = report["data_manifest"]["download_parameters"]
        params["qualified_contract"].update(symbol=symbol, con_id=con_id)
        proof = params["application_ibkr_paper_import_attestation"]
        proof.update(symbol=symbol, instrument_id=symbol, con_id=con_id)
        config_path = report_path.parent.parent / "config.json"
        config = json.loads(config_path.read_text())
        config["ibkr_paper_import_attestation"] = proof
        config_path.write_text(json.dumps(config))
        report_path.write_text(json.dumps(report))
        write_report_integrity_sidecar(report_path)
    order = ProposedOrder(instrument_id=symbol, side="BUY", integer_qty=quantity,
                          order_type="limit", time_in_force="DAY", limit_price=Decimal("400"))
    setup = PaperOrderSetup.model_validate({
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "900", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2,
    })
    save_paper_order_draft(runtime_root=tmp_path, account_ref="DU123", run_id="bt_20261031_120000_abcdef123456",
                           version_id=version.version_id, order=order, setup=setup)
    scope = PaperRuntimeScope("IBKR", "DU123", Environment.PAPER)
    kwargs = dict(runtime_root=tmp_path, scope=scope, runtime_config=_runtime_config(tmp_path, "DU123"),
                  runtime_dependencies=_runtime_dependencies(), sandbox=PythonSandbox("sha256:" + "a" * 64),
                  control_authorizer=lambda *_: False, snapshot_reader=IbkrManagedRiskSnapshotSource(account_ref="DU123"),
                  quote_reader=lambda _: {}, fx_quote_reader=lambda _: {}, metrics_reader=None,
                  fx_contract_ids={}, now=lambda: datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc),
                  run_id="draft-run", deployment_id="draft-deployment", user_action_id="action",
                  previous_bar_reader=lambda *_: None, price_buffer_fraction=Decimal("0.05"), estimated_fees=Decimal("1"),
                  initial_checkpoint={"warmup_count": 2})
    composition = build_paper_draft_composition(**kwargs)
    assert composition.scope == scope
    assert composition.manager.status() is None
    builder = composition.manager.runtime_factory.session_builder
    identity = {"deployment_id": "draft-deployment", "account_ref": "DU123", "environment": "paper",
                "strategy_version_hash": version.version_id}
    journal = SQLiteEventJournal(kwargs["runtime_config"].database_path)
    builder._create_or_verify_run(journal, identity)
    assert journal.get_checkpoint("draft-run") == {"warmup_count": 2}
    builder._create_or_verify_run(journal, identity)
    builder.initial_checkpoint = {"warmup_count": 3}
    with pytest.raises(PythonUploadPaperSessionBuildError, match="different initial"):
        builder._create_or_verify_run(journal, identity)
    builder.initial_checkpoint = {"warmup_count": 2}
    provider = builder.dispatch_risk_facts
    assert provider.order_policy.order == order
    assert provider.risk_grant.max_order_notional == Decimal("900")
    assert provider.risk_grant.strategy_budget == Decimal("900")
    assert provider.risk_grant.valid_from == setup.valid_from
    assert provider.contracts[symbol].con_id == con_id
    assert provider.snapshot_reader._runtime is None
    with pytest.raises(ValueError, match="same IBKR PAPER account"):
        build_paper_draft_composition(**{**kwargs, "scope": PaperRuntimeScope("IBKR", "DU999", Environment.PAPER)})
    history_config = IBKRLocalConfig(profile="paper", host="127.0.0.1", port=4002,
                                     account="DU123", readonly=True)
    with_history = build_paper_draft_composition(**{**kwargs, "previous_bar_reader": None,
                                                     "history_config": history_config})
    history_reader = with_history.manager.runtime_factory.session_builder.dispatch_risk_facts.metrics_reader.previous_bar_reader
    assert history_reader.__self__.contract.symbol == symbol
    assert history_reader.__self__.contract.con_id == con_id

    # Drive the composition's actual proposal and send-risk providers with
    # fixture account/quote facts and a recording sender; no SDK is started.
    instant = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    provider.now = lambda: instant
    snapshot = {"capture_complete": True, "reconciled": True, "account_ref": "DU123",
                "environment": "paper", "facts_sha256": "b" * 64, "completed_at_utc": instant.isoformat(),
                "positions": [], "open_orders": [], "account_summary": [
                    {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
                    {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
                    {"tag": "AvailableFunds", "currency": "USD", "value": "5000"}]}
    provider.snapshot_reader = lambda: snapshot
    response = quote(observed=instant)
    response["qualified_contract"].update(symbol=symbol, con_id=con_id)
    provider.quote_reader = lambda _: response
    proposal = OutboxProposal(
        run_id="draft-run", event_id="event", seq=1, proposal_index=0,
        proposal={"side": "BUY", "integer_qty": quantity, "order_type": "limit",
                  "time_in_force": "DAY", "limit_price": "400"},
        identity={"account_ref": "DU123", "environment": "paper", "strategy_version_hash": version.version_id,
                  "deployment_id": "draft-deployment"},
        bar=_bar().model_copy(update={"instrument_id": symbol, "end_at": datetime(2026, 10, 30, 20, tzinfo=timezone.utc)}),
    )
    authorizer = RiskAwareProposalAuthorizer(provider.proposal_inputs)
    snapshot["account_summary"][1]["value"] = "100"
    rejected = authorizer(proposal)
    assert rejected.intent is None and "available_cash" in rejected.decision.reason_codes
    snapshot["account_summary"][1]["value"] = "5000"
    decision = authorizer(proposal)
    assert decision.decision.allowed and decision.intent.integer_qty == quantity
    ledger = SQLiteOrderLedger(kwargs["runtime_config"].database_path)
    ledger.set_execution_control(broker="IBKR", account_ref="DU123", environment=Environment.PAPER,
                                 state=ExecutionControlState.RUNNING)
    record = ledger.create_intent(decision.intent)
    sends = []
    dispatch = PaperOrderDispatchService(ledger=ledger, facts_provider=provider,
                                         sender=lambda value, **_: sends.append(value), now=lambda: instant)
    assert dispatch.dispatch(record.intent_id).status == "SEND_STARTED"
    assert len(sends) == 1 and sends[0].instrument_id == symbol and sends[0].integer_qty == quantity
    from dataclasses import replace
    with pytest.raises(IbkrPaperRiskFactsError, match="order does not match"):
        authorizer(replace(proposal, proposal={**proposal.proposal, "integer_qty": quantity + 1}))
