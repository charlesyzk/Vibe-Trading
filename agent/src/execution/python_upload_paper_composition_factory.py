"""Explicit offline composition for one frozen Python-upload IBKR PAPER version."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.contracts import SourceKind, is_sha256
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperRuntimeConfig,
    IbkrPaperRuntimeDependencies,
    IbkrPaperRuntimeFactory,
)
from src.execution.managed_python_paper_deployment import ManagedPythonPaperDeploymentFactory
from src.execution.outbox_dispatch import ProposalRiskInputs
from src.execution.paper_execution_composition import PaperExecutionComposition
from src.execution.paper_execution_manager import (
    ControlAuthorizer,
    PaperExecutionManager,
    PaperRuntimeScope,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.python_sandbox import PythonSandbox
from src.execution.python_upload_paper_session_builder import (
    DispatchRiskFactsProvider,
    PythonUploadPaperSessionBuilder,
)
from src.execution.versions import StrategyVersionStore


class PythonUploadPaperCompositionError(ValueError):
    """Explicit Python-upload PAPER composition inputs are incomplete or unsafe."""


ContractIdentityResolver = Callable[[str], Any]


def _callback_before_session(_callback: Any) -> None:
    """Reject callback delivery until the managed upload session binds its router."""
    raise RuntimeError("PAPER callback arrived before its frozen Python-upload session was bound")


def _validate_frozen_python_upload_version(
    version_store: StrategyVersionStore,
    version_id: str,
):
    if not isinstance(version_store, StrategyVersionStore):
        raise TypeError("version_store must be a trusted StrategyVersionStore")
    if not isinstance(version_id, str) or not is_sha256(version_id):
        raise ValueError("version_id must be a lowercase immutable SHA-256 ID")
    try:
        record = version_store.get_version(version_id)
        source = version_store.read_upload_source(version_id)
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.strategy_source_policy import validate_strategy_source
        import backtrader

        if (
            record.version_id != version_id
            or record.version_hash != version_id
            or record.source_kind is not SourceKind.PYTHON_UPLOAD
            or record.upload_artifact != f"sources/{record.source_hash}.py"
            or record.template_artifact is not None
            or record.spec.source_kind is not SourceKind.PYTHON_UPLOAD
            or record.spec.source_artifact_id != f"upload-{record.source_hash[:16]}"
            or record.compiler_version != PYTHON_WORKER_COMPILER_VERSION
            or record.engine_version != f"backtrader/{backtrader.__version__}"
        ):
            raise ValueError("frozen upload provenance/runtime")
        validate_strategy_source(source.decode("utf-8", errors="strict"))
        return record
    except Exception as exc:
        raise PythonUploadPaperCompositionError(
            "version_id must identify an installed frozen Python-upload PAPER version"
        ) from exc


def build_frozen_python_upload_paper_composition(
    *,
    scope: PaperRuntimeScope,
    runtime_config: IbkrPaperRuntimeConfig,
    runtime_dependencies: IbkrPaperRuntimeDependencies,
    version_store: StrategyVersionStore,
    version_id: str,
    run_id: str,
    deployment_id: str,
    sandbox: PythonSandbox,
    proposal_risk_inputs: Callable[[Any], ProposalRiskInputs],
    dispatch_risk_facts: DispatchRiskFactsProvider,
    feeds: Mapping[str, PaperCompletedBarFeed],
    data_by_con_id: Mapping[int, Any],
    resolve_contract_identity: ContractIdentityResolver,
    control_authorizer: ControlAuthorizer,
    now: Callable[[], datetime],
    queue_size: int = 256,
    idle_poll_seconds: float = 0.05,
) -> PaperExecutionComposition:
    """Build one offline, exact-version Python-upload PAPER composition.

    This constructs local collaborators only. The SDK process remains stopped,
    the account lease remains unheld, and the app does not start dispatch.
    """
    if not isinstance(scope, PaperRuntimeScope):
        raise TypeError("scope must be a trusted PaperRuntimeScope")
    if not isinstance(runtime_config, IbkrPaperRuntimeConfig):
        raise TypeError("runtime_config must be an explicit IbkrPaperRuntimeConfig")
    if not isinstance(runtime_dependencies, IbkrPaperRuntimeDependencies):
        raise TypeError("runtime_dependencies must be explicit IbkrPaperRuntimeDependencies")
    if scope.account_ref != runtime_config.account_ref:
        raise PythonUploadPaperCompositionError("scope account_ref differs from the PAPER runtime configuration")
    if not isinstance(sandbox, PythonSandbox):
        raise TypeError("sandbox must be an explicitly configured PythonSandbox")
    if not callable(control_authorizer):
        raise TypeError("control_authorizer is required")

    record = _validate_frozen_python_upload_version(version_store, version_id)
    session_builder = PythonUploadPaperSessionBuilder(
        version_store=version_store,
        version_id=record.version_id,
        run_id=run_id,
        deployment_id=deployment_id,
        proposal_risk_inputs=proposal_risk_inputs,
        dispatch_risk_facts=dispatch_risk_facts,
        sandbox=sandbox,
        feeds=feeds,
        data_by_con_id=data_by_con_id,
        resolve_contract_identity=resolve_contract_identity,
        now=now,
        queue_size=queue_size,
        idle_poll_seconds=idle_poll_seconds,
    )
    # Refuse incompatible frozen policies and contract/feed maps before the
    # composition can report readiness. These checks are local and side-effect
    # free; no provider is queried and no runtime process is started.
    checked_record = session_builder._load_frozen_upload()
    session_builder._validate_feed_contracts(checked_record)
    safe_runtime_dependencies = replace(
        runtime_dependencies,
        callback_sink=_callback_before_session,
    )
    ibkr_factory = IbkrPaperRuntimeFactory(
        config=runtime_config,
        dependencies=safe_runtime_dependencies,
    )
    deployment_factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=ibkr_factory,
        session_builder=session_builder,
        strategy_version_id=record.version_id,
    )
    readiness = PaperExecutionReadiness(
        strategy_session_builder=True,
        attributable_control_authorizer=True,
        account_and_reconciliation_facts=True,
        market_data_and_verified_contracts=True,
        risk_and_cost_facts=True,
    )
    manager = PaperExecutionManager(
        runtime_factory=deployment_factory,
        control_authorizer=control_authorizer,
        startup_readiness=readiness,
    )
    return PaperExecutionComposition(manager=manager, readiness=readiness, scope=scope)


__all__ = [
    "PythonUploadPaperCompositionError",
    "build_frozen_python_upload_paper_composition",
]
