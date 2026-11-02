"""Local app wiring for an explicitly saved IBKR PAPER draft."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.config.paths import get_runtime_root
from src.execution.contracts import Environment, canonical_sha256
from src.execution.ibkr_paper_risk_facts import IbkrLocalPaperQuoteReader, IbkrManagedRiskSnapshotSource
from src.execution.ibkr_completed_bar_source import IbkrPaperCompletedBarSource
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperEndpointProfile, IbkrPaperRuntimeConfig, IbkrPaperRuntimeDependencies,
)
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.paper_control_grants import PaperControlGrantStore
from src.execution.paper_draft_composition import build_paper_draft_composition
from src.execution.paper_deployment_receipt import deployment_granted_at
from src.execution.paper_draft_control_authorizer import PaperDraftControlAuthorizer
from src.execution.paper_draft_runner import PaperDraftRunner
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_integration_test_composition import _ibkr_runtime_paths, _setting
from src.execution.paper_order_draft import load_paper_order_draft
from src.execution.paper_order_preparation import PaperOrderSetup
from src.execution.paper_strategy_warmup import build_paper_warmup
from src.execution.python_sandbox import PythonSandbox
from src.session.models import Principal
from src.trading.connectors.ibkr.local import load_config


def build_local_paper_draft_composition(scope: PaperRuntimeScope, principal: Principal):
    """Construct local collaborators only; connecting still requires start."""
    if not isinstance(principal, Principal) or not principal.paper_operator:
        raise ValueError("an attributable operator is required")
    root = get_runtime_root()
    draft = load_paper_order_draft(runtime_root=root, account_ref=scope.account_ref)
    if draft is None:
        raise ValueError("no PAPER draft is saved")
    setup = PaperOrderSetup.model_validate(draft["setup"])
    config = load_config()
    if (config.profile != "paper" or config.readonly is not True or config.account != scope.account_ref
            or config.port not in {4002, 7497} or config.host.strip().lower() not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("local Gateway settings do not match the PAPER account")
    runtime_home = root / "paper-integration-test"
    runtime_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = runtime_home / "execution.sqlite"
    ledger = SQLiteOrderLedger(database)
    runtime_python, runtime_project = _ibkr_runtime_paths(
        backend_python=Path(sys.executable), repository_root=Path(__file__).resolve().parents[3], windows=os.name == "nt",
    )
    runtime_config = IbkrPaperRuntimeConfig(
        python=runtime_python, runtime_cwd=runtime_project, gateway_host=config.host, gateway_port=config.port,
        paper_endpoint_profile=(IbkrPaperEndpointProfile.IB_GATEWAY_PAPER if config.port == 4002 else IbkrPaperEndpointProfile.TWS_PAPER),
        client_id=6138, account_ref=scope.account_ref, database_path=database, lock_directory=runtime_home / "owner-lock",
    )
    runtime_ref = SimpleNamespace(runtime=None)

    def verified_snapshot() -> bool:
        runtime = runtime_ref.runtime
        try:
            snapshot = runtime.snapshot_store.complete_snapshot(runtime._latest_snapshot_id)
            return bool(snapshot.get("capture_complete") is True and snapshot.get("reconciled") is True
                        and snapshot.get("account_ref") == scope.account_ref
                        and snapshot.get("environment") == Environment.PAPER.value
                        and snapshot.get("facts_sha256") and isinstance(snapshot.get("reconciliation_result"), dict))
        except Exception:
            return False

    def activation_check() -> bool:
        now = datetime.now(timezone.utc)
        return bool(verified_snapshot() and setup.valid_from <= now < setup.valid_until)

    def quiesce_check() -> bool:
        runtime = runtime_ref.runtime
        return bool(runtime is not None and runtime.callback_worker.failure is None
                    and runtime.callback_pump.events.empty() and not runtime.order_index.pending_callbacks()
                    and not runtime.order_index.pending_inbox())

    dependencies = IbkrPaperRuntimeDependencies(
        callback_sink=lambda _: (_ for _ in ()).throw(RuntimeError("callback consumer is not bound")),
        recovery_check=verified_snapshot, activation_check=activation_check, quiesce_check=quiesce_check,
        intent_loader=ledger.get_intent, contract_loader=lambda _: None,
    )
    from src.execution.python_worker_factory import configured_worker_identity, create_worker
    image = configured_worker_identity() or _setting("VIBE_PYTHON_WORKER_IMAGE")
    if not image:
        raise RuntimeError("isolated Python worker image is not configured")
    sandbox = create_worker(image) if os.environ.get("VIBE_WASI_WORKER_ROOT", "").strip() else PythonSandbox(image, context=_setting("DOCKER_CONTEXT") or None)
    warmup = build_paper_warmup(runtime_root=root, run_id=draft["run_id"], version_id=draft["strategy_version_id"],
                                expected_manifest_hash=draft["data_manifest_hash"], sandbox=sandbox)
    quote_reader = IbkrLocalPaperQuoteReader(config=config, account_ref=scope.account_ref)
    deployment_hash = canonical_sha256({key: draft[key] for key in
                                        ("strategy_version_id", "run_id", "order", "setup", "report_sha256")})
    granted_at = deployment_granted_at(database=runtime_home / "deployment-receipts.sqlite", scope=scope,
                                       principal=principal, draft=draft, now=datetime.now(timezone.utc))
    control_authorizer = PaperDraftControlAuthorizer(
        scope=scope, principal=principal, setup=setup, granted_at=granted_at,
        now=lambda: datetime.now(timezone.utc), fallback=PaperControlGrantStore(root / "paper-control-grants.sqlite"),
    )
    composition = build_paper_draft_composition(
        runtime_root=root, scope=scope, runtime_config=runtime_config, runtime_dependencies=dependencies,
        sandbox=sandbox,
        control_authorizer=control_authorizer,
        snapshot_reader=IbkrManagedRiskSnapshotSource(account_ref=scope.account_ref), quote_reader=quote_reader,
        fx_quote_reader=quote_reader.fx_quote, metrics_reader=None, fx_contract_ids={},
        now=lambda: datetime.now(timezone.utc), run_id=f"paper-draft-{deployment_hash[:24]}",
        deployment_id=f"paper-draft:{deployment_hash}", user_action_id=principal.subject,
        history_config=config, price_buffer_fraction=Decimal("0.05"), estimated_fees=Decimal("1.00"),
        initial_checkpoint=warmup.checkpoint,
        granted_at=granted_at,
    )
    original_factory = composition.manager.runtime_factory.runtime_factory

    def runtime_factory(actual_scope):
        runtime = original_factory(actual_scope)
        runtime_ref.runtime = runtime
        return runtime

    composition.manager.runtime_factory.runtime_factory = runtime_factory
    provider = composition.manager.runtime_factory.session_builder.dispatch_risk_facts
    contract = provider.contracts[draft["order"]["instrument_id"]]
    source = IbkrPaperCompletedBarSource(config=config, account_ref=scope.account_ref, contract=contract,
                                         now=lambda: datetime.now(timezone.utc), duration="1 M")
    runner = PaperDraftRunner(manager=composition.manager, scope=scope, runtime_root=root, draft=draft,
                              warmup=warmup, source=source, now=lambda: datetime.now(timezone.utc))
    return replace(composition, draft_runner=runner)
