"""Compose a saved PAPER draft with explicit host-side execution services."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping
from decimal import Decimal

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.contracts import Environment
from src.execution.ibkr_callback_pump import ContractIdentity
from src.execution.ibkr_completed_bar_source import IbkrPaperCompletedBarSource
from src.execution.ibkr_paper_risk_facts import IbkrManagedRiskSnapshotSource, IbkrPaperRiskFactsProvider
from src.execution.ibkr_paper_runtime_factory import IbkrPaperRuntimeConfig, IbkrPaperRuntimeDependencies
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_draft import load_paper_order_draft
from src.execution.paper_order_preparation import PaperOrderSetup, prepare_verified_paper_order
from src.execution.paper_strategy_metrics import LedgerComputedStrategyMetricsReader, LedgerStrategyMetricsReader
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.python_upload_paper_composition_factory import build_frozen_python_upload_paper_composition
from src.execution.risk import ProposedOrder
from src.execution.versions import StrategyVersionStore


def build_paper_draft_composition(
    *, runtime_root: str | Path, scope: PaperRuntimeScope, runtime_config: IbkrPaperRuntimeConfig,
    runtime_dependencies: IbkrPaperRuntimeDependencies, sandbox, control_authorizer,
    snapshot_reader: IbkrManagedRiskSnapshotSource, quote_reader: Callable, fx_quote_reader: Callable,
    metrics_reader: Callable | None, fx_contract_ids: Mapping[str, int], now: Callable[[], datetime],
    run_id: str, deployment_id: str, user_action_id: str, previous_bar_reader: Callable | None = None,
    history_config=None, price_buffer_fraction: Decimal | None = None, estimated_fees: Decimal | None = None,
    initial_checkpoint: dict | None = None,
    granted_at: datetime | None = None,
):
    """Build the local graph from a saved draft without starting or connecting.

    The host must supply the real reconciliation checks, quotes, risk metrics,
    and management authorizer. This factory does not install itself in the app.
    """
    if (not isinstance(scope, PaperRuntimeScope) or scope.broker != "IBKR"
            or scope.environment is not Environment.PAPER or scope.account_ref != runtime_config.account_ref):
        raise ValueError("draft and runtime require the same IBKR PAPER account")
    if not isinstance(snapshot_reader, IbkrManagedRiskSnapshotSource) or snapshot_reader.account_ref != scope.account_ref:
        raise ValueError("a matching managed PAPER snapshot source is required")
    root = Path(runtime_root)
    draft = load_paper_order_draft(runtime_root=root, account_ref=scope.account_ref)
    if draft is None:
        raise ValueError("no saved PAPER order draft is available")
    order = ProposedOrder.model_validate(draft["order"])
    setup = PaperOrderSetup.model_validate(draft["setup"])
    prepared = prepare_verified_paper_order(runtime_root=root, run_id=draft["run_id"],
                                            version_id=draft["strategy_version_id"],
                                            account_ref=scope.account_ref, order=order)
    if (prepared.report_sha256 != draft["report_sha256"] or prepared.data_manifest_hash != draft["data_manifest_hash"]
            or prepared.contract.con_id != draft["contract_id"]):
        raise ValueError("PAPER draft evidence changed while building the composition")
    grant = prepared.build_risk_grant(setup, grant_id=f"paper:{deployment_id}",
                                      user_action_id=user_action_id, granted_at=granted_at or now())
    contract = prepared.contract
    if previous_bar_reader is not None and history_config is not None:
        raise ValueError("choose one previous-bar source")
    if history_config is not None:
        previous_bar_reader = IbkrPaperCompletedBarSource(
            config=history_config, account_ref=scope.account_ref, contract=contract, now=now, duration="1 M",
        ).completed_bar
    # Ensure the shared ledger exists before the count reader opens it read-only.
    SQLiteOrderLedger(runtime_config.database_path)
    if metrics_reader is None:
        if price_buffer_fraction is None or estimated_fees is None:
            raise ValueError("explicit price buffer and estimated fees are required")
        metrics = LedgerComputedStrategyMetricsReader(
            database=runtime_config.database_path, account_ref=scope.account_ref,
            previous_bar_reader=previous_bar_reader, price_buffer_fraction=price_buffer_fraction,
            estimated_fees=estimated_fees,
        )
    else:
        metrics = LedgerStrategyMetricsReader(database=runtime_config.database_path,
                                              account_ref=scope.account_ref, source=metrics_reader,
                                              previous_bar_reader=previous_bar_reader)
    provider = IbkrPaperRiskFactsProvider(
        account_ref=scope.account_ref, strategy_version_hash=prepared.policy.strategy_version_hash,
        risk_grant=grant, contracts={contract.instrument_id: contract}, fx_contract_ids=fx_contract_ids,
        snapshot_reader=snapshot_reader, quote_reader=quote_reader, fx_quote_reader=fx_quote_reader,
        metrics_reader=metrics, order_policy=prepared.policy, now=now,
    )

    def feeds():
        feed = PaperCompletedBarFeed(instrument_id=contract.instrument_id, timezone="America/New_York",
                                     currency="USD", calendar_id="us_equity_rth", now=now, idle_poll_seconds=0.05)
        return {contract.instrument_id: feed}, {contract.con_id: feed}

    feed_map, by_con_id = feeds()
    identity = ContractIdentity(contract_id=contract.con_id, symbol=contract.symbol,
                                security_type=contract.security_type, currency=contract.currency)
    dependencies = replace(runtime_dependencies, contract_loader=lambda key: contract if key == contract.instrument_id else None)
    composition = build_frozen_python_upload_paper_composition(
        scope=scope, runtime_config=runtime_config, runtime_dependencies=dependencies,
        version_store=StrategyVersionStore(root / "strategy-versions"), version_id=prepared.policy.strategy_version_hash,
        run_id=run_id, deployment_id=deployment_id, sandbox=sandbox,
        proposal_risk_inputs=provider.proposal_inputs, dispatch_risk_facts=provider,
        feeds=feed_map, data_by_con_id=by_con_id, feed_factory=feeds,
        resolve_contract_identity=lambda key: identity if key == contract.instrument_id else None,
        control_authorizer=control_authorizer, now=now,
        initial_checkpoint=initial_checkpoint,
    )
    composition.manager.bind_stopped_activity(scope, SQLiteOrderLedger(runtime_config.database_path))
    return composition
