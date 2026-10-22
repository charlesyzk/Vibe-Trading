"""Explicit, offline app composition for one frozen NL2 IBKR PAPER version.

This factory assembles trusted collaborators only. It does not start the
managed runtime, acquire an account lease, or open a Gateway connection. The
API remains inert unless the returned composition is explicitly installed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

import backtrader

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.bt_runtime.strategies.sma import (
    TRUSTED_TEMPLATE_COMPILER_VERSION,
    TRUSTED_TEMPLATE_SOURCE,
    TRUSTED_TEMPLATE_SOURCE_SHA256,
    compile_strategy,
)
from src.execution.contracts import PositionMode, SourceKind, is_sha256
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
from src.execution.trusted_template_paper_session_builder import (
    DispatchRiskFactsProvider,
    TrustedTemplatePaperSessionBuilder,
)
from src.execution.versions import StrategyVersionStore


class NL2PaperCompositionError(ValueError):
    """Explicit NL2 PAPER composition inputs do not form one safe deployment."""


ContractIdentityResolver = Callable[[str], Any]


def _callback_before_session(_callback: Any) -> None:
    """Reject callback delivery until the managed session installs its router."""
    raise RuntimeError("PAPER callback arrived before its frozen strategy session was bound")


def _validate_frozen_nl2_version(
    version_store: StrategyVersionStore,
    version_id: str,
):
    if not isinstance(version_store, StrategyVersionStore):
        raise TypeError("version_store must be a trusted StrategyVersionStore")
    if not isinstance(version_id, str) or not is_sha256(version_id):
        raise ValueError("version_id must be a lowercase immutable SHA-256 ID")
    try:
        record = version_store.get_version(version_id)
        source = version_store.read_template_source(version_id)
        if (
            record.version_id != version_id
            or record.version_hash != version_id
            or record.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE
            or record.upload_artifact is not None
            or record.template_artifact is None
            or record.source_hash != TRUSTED_TEMPLATE_SOURCE_SHA256
            or source != TRUSTED_TEMPLATE_SOURCE
            or record.compiler_version != TRUSTED_TEMPLATE_COMPILER_VERSION
            or record.engine_version != f"backtrader/{backtrader.__version__}"
            or record.spec.position_policy.mode is not PositionMode.TARGET_FRACTION
            or record.spec.template_version != "1.0"
            or record.spec.template_id not in {"sma_cross", "rsi_threshold"}
            or len(record.spec.instrument_ids) != 1
        ):
            raise ValueError("frozen version provenance or managed policy")
        compile_strategy(record.spec)
        return record
    except Exception as exc:
        raise NL2PaperCompositionError(
            "version_id must identify an installed frozen NL2 PAPER template version"
        ) from exc


def build_frozen_nl2_paper_composition(
    *,
    scope: PaperRuntimeScope,
    runtime_config: IbkrPaperRuntimeConfig,
    runtime_dependencies: IbkrPaperRuntimeDependencies,
    version_store: StrategyVersionStore,
    version_id: str,
    run_id: str,
    deployment_id: str,
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
    """Build one explicitly configured, pinned NL2 PAPER app composition.

    All account, provider, contract, authorization and runtime facts are
    required inputs. The callback sink from ``runtime_dependencies`` is
    replaced by a temporary fail-closed sink; ``TrustedTemplatePaperSessionBuilder``
    installs its bounded callback router before the SDK process can start.
    """
    if not isinstance(scope, PaperRuntimeScope):
        raise TypeError("scope must be a trusted PaperRuntimeScope")
    if not isinstance(runtime_config, IbkrPaperRuntimeConfig):
        raise TypeError("runtime_config must be an explicit IbkrPaperRuntimeConfig")
    if not isinstance(runtime_dependencies, IbkrPaperRuntimeDependencies):
        raise TypeError("runtime_dependencies must be explicit IbkrPaperRuntimeDependencies")
    if scope.account_ref != runtime_config.account_ref:
        raise NL2PaperCompositionError("scope account_ref differs from the PAPER runtime configuration")
    if not callable(control_authorizer):
        raise TypeError("control_authorizer is required")

    record = _validate_frozen_nl2_version(version_store, version_id)

    # Build a trusted strategy session before readiness can be declared. Its
    # constructor validates feeds, contract mappings, providers, identity and
    # frozen template policy without calling mutable providers.
    session_builder = TrustedTemplatePaperSessionBuilder(
        version_store=version_store,
        version_id=record.version_id,
        run_id=run_id,
        deployment_id=deployment_id,
        proposal_risk_inputs=proposal_risk_inputs,
        dispatch_risk_facts=dispatch_risk_facts,
        feeds=feeds,
        data_by_con_id=data_by_con_id,
        resolve_contract_identity=resolve_contract_identity,
        now=now,
        queue_size=queue_size,
        idle_poll_seconds=idle_poll_seconds,
    )

    # Do not trust a pre-session callback target supplied by the caller. The
    # managed session replaces this sink while composing the runtime, before
    # any subprocess is allowed to start.
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


__all__ = ["NL2PaperCompositionError", "build_frozen_nl2_paper_composition"]
