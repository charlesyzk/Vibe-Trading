"""Read-only status surface for the managed IBKR PAPER runtime."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.security import require_auth, require_event_stream_auth
from src.api.state import _get_paper_execution_manager, _get_paper_execution_readiness
from src.api.state import _set_paper_execution_composition
from src.config.paths import get_runtime_root
from src.execution.order_ledger import ExecutionControlState, InvalidActivityCursor
from src.execution.contracts import is_sha256
from src.execution.paper_execution_manager import (
    PaperExecutionManagerError,
    PaperRuntimeAuthorizationError,
    PaperRuntimeScope,
    PaperRuntimeStatus,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.paper_execution_composition import PaperExecutionComposition
from src.execution.paper_integration_test_composition import PaperSignalSessionMismatch
from src.execution.paper_strategy_candidates import list_paper_strategy_candidates
from src.execution.paper_order_preparation import PaperOrderSetup, prepare_verified_paper_order, suggest_paper_entry_window
from src.execution.paper_order_draft import load_paper_order_draft, save_paper_order_draft
from src.execution.paper_draft_runner import PaperDraftRunError
from src.execution.local_paper_gateway_discovery import PaperGatewayDiscoveryError, discover_local_paper_gateway
from src.execution.risk import ProposedOrder
from src.execution.paper_strategy_deployment import (
    PaperStrategyDeploymentError,
    commit_paper_strategy_selection,
    paper_execution_ledger_is_empty,
    prepare_paper_strategy_selection,
    rollback_paper_strategy_selection,
)
from src.session.models import Principal


class PaperExecutionStatusResponse(BaseModel):
    configured: bool
    runtime_factory_registered: bool = False
    composition_ready: bool = False
    missing_requirements: list[str] = Field(default_factory=list)
    phase: str
    control_state: Optional[str] = None
    dispatch_gate_open: bool = False
    owner_lease_held: bool = False
    reason: Optional[str] = None
    last_error_code: Optional[str] = None
    strategy_version_id: Optional[str] = None


class PaperExecutionActivityResponse(BaseModel):
    broker: str
    environment: str
    control_state: str
    control_reason: Optional[str] = None
    orders: list[dict[str, Any]] = Field(default_factory=list)
    history_limit: int
    latest_activity_seq: int
    recovery: dict[str, int]
    risk: dict[str, Any]
    positions: dict[str, Any]


class PaperStartRequest(BaseModel):
    """Echo the version already pinned by trusted app composition."""

    # Legacy start requests may still include ignored scope-shaped fields;
    # only the trusted factory's exact pinned version is actionable here.
    model_config = ConfigDict(extra="ignore")

    strategy_version_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PaperCancelOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(min_length=1, max_length=200)

    @field_validator("intent_id")
    @classmethod
    def nonblank_intent_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("intent_id must not be blank")
        return value


class PaperStrategySelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


class PaperOrderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    strategy_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    order: ProposedOrder
    setup: PaperOrderSetup | None = None


class PaperCancelOrderResponse(BaseModel):
    intent_id: str
    state: str


def _manager(app):
    """Resolve the manager owned by this FastAPI application instance."""
    return _get_paper_execution_manager(app)


def _readiness(app):
    return _get_paper_execution_readiness(app)


def _active_scope(manager) -> PaperRuntimeScope:
    active = manager.status()
    if active is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="no managed PAPER runtime is active",
        )
    return active.scope


def _management_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PaperRuntimeAuthorizationError):
        return HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="an attributable human principal and matching PAPER control grant are required",
        )
    if isinstance(exc, PaperExecutionManagerError):
        return HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="PAPER runtime did not complete the requested safe state transition",
        )
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail="PAPER runtime control failed closed",
    )


def _status_response(manager, readiness) -> PaperExecutionStatusResponse:
    configured = manager.runtime_factory is not None
    startup_readiness = getattr(manager, "startup_readiness", None)
    has_bound_readiness = isinstance(startup_readiness, PaperExecutionReadiness)
    readiness_mismatch = has_bound_readiness and startup_readiness is not readiness
    # The manager's bound declaration is the one start() will enforce. If
    # app.state has drifted, report that conflict and fail closed rather than
    # letting the UI show a readiness that cannot pass the actual startup gate.
    effective_readiness = startup_readiness if has_bound_readiness else readiness
    missing_requirements = list(effective_readiness.missing_requirements)
    if readiness_mismatch:
        missing_requirements.append("readiness_binding_mismatch")
    if not configured:
        missing_requirements.insert(0, "ibkr_runtime_factory")
    composition_ready = (
        configured
        and effective_readiness.composition_ready
        and not readiness_mismatch
    )
    pinned_version_id = getattr(manager.runtime_factory, "strategy_version_id", None)
    managed_status = manager.status()
    if managed_status is None:
        return PaperExecutionStatusResponse(
            configured=configured,
            runtime_factory_registered=configured,
            composition_ready=composition_ready,
            missing_requirements=missing_requirements,
            phase="STOPPED" if configured else "NOT_CONFIGURED",
            reason="runtime_not_started" if configured else "runtime_composition_not_configured",
            last_error_code=getattr(manager, "last_start_error_code", None),
            strategy_version_id=pinned_version_id,
        )
    state = managed_status.control_state
    state_value = state.value if isinstance(state, ExecutionControlState) else str(state)
    return PaperExecutionStatusResponse(
        configured=configured,
        runtime_factory_registered=configured,
        composition_ready=composition_ready,
        missing_requirements=missing_requirements,
        phase=managed_status.phase,
        control_state=state_value,
        dispatch_gate_open=state is ExecutionControlState.RUNNING,
        owner_lease_held=managed_status.owner_lease_held,
        reason=managed_status.reason,
        last_error_code=managed_status.last_error_code,
        strategy_version_id=pinned_version_id,
    )


def register_paper_execution_routes(app: FastAPI) -> None:
    @app.get("/api/execution/paper/suggest-window", dependencies=[Depends(require_auth)])
    async def suggest_paper_window() -> dict[str, str]:
        """Suggest a regular-session window without authorizing or placing an order."""
        try:
            start, end = suggest_paper_entry_window(datetime.now(timezone.utc))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="PAPER trading calendar is unavailable") from exc
        return {"valid_from": start.isoformat(), "valid_until": end.isoformat()}

    @app.get(
        "/api/execution/paper/status",
        response_model=PaperExecutionStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_paper_execution_status(request: Request) -> PaperExecutionStatusResponse:
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        return _status_response(manager, readiness)

    @app.get(
        "/api/execution/paper/activity",
        response_model=PaperExecutionActivityResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_paper_execution_activity(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> PaperExecutionActivityResponse:
        """Read persisted PAPER activity under the already trusted scope."""
        manager = _manager(request.app)
        try:
            return PaperExecutionActivityResponse.model_validate(
                manager.activity_snapshot(limit=limit)
            )
        except Exception as exc:
            raise _management_error(exc) from exc

    @app.get(
        "/api/execution/paper/strategy-candidates",
        dependencies=[Depends(require_auth)],
    )
    async def get_paper_strategy_candidates() -> dict[str, Any]:
        """List frozen versions with verified reports and exact PAPER eligibility."""
        try:
            return list_paper_strategy_candidates()
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PAPER strategy candidate catalog is unavailable",
            ) from exc

    @app.post("/api/execution/paper/order-preview")
    async def preview_paper_order(
        request: Request,
        body: PaperOrderPreviewRequest,
        _principal: Principal = Depends(require_auth),
    ) -> dict[str, Any]:
        """Check frozen order and historical proof without connecting or authorizing dispatch."""
        scope = getattr(request.app.state, "paper_execution_scope", None)
        if not isinstance(scope, PaperRuntimeScope) or scope.broker != "IBKR" or scope.environment.value != "paper":
            raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="trusted IBKR PAPER account is not configured")
        try:
            prepared = prepare_verified_paper_order(
                runtime_root=get_runtime_root(), run_id=body.run_id,
                version_id=body.strategy_version_id, account_ref=scope.account_ref, order=body.order,
            )
            if body.setup is not None:
                body.setup.validate_order(prepared.policy.order)
        except ValueError as exc:
            raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="PAPER order evidence could not be verified") from exc
        return {
            "strategy_version_id": prepared.policy.strategy_version_hash,
            "run_id": prepared.run_id,
            "order": prepared.policy.order.model_dump(mode="json"),
            "contract": {
                "symbol": prepared.contract.symbol, "con_id": prepared.contract.con_id,
                "security_type": prepared.contract.security_type, "exchange": prepared.contract.exchange,
                "primary_exchange": prepared.contract.primary_exchange, "currency": prepared.contract.currency,
            },
            "backtest_report_sha256": prepared.report_sha256,
            "data_manifest_hash": prepared.data_manifest_hash,
            "setup": body.setup.model_dump(mode="json") if body.setup is not None else None,
            "static_notional_checked": body.setup is not None and prepared.policy.order.order_type == "limit",
            "live_risk_checked": False,
            "ready_for_submission": False,
            "order_submission_performed": False,
        }

    @app.get("/api/execution/paper/order-draft")
    async def get_paper_order_draft(request: Request, _principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        scope = getattr(request.app.state, "paper_execution_scope", None)
        if not isinstance(scope, PaperRuntimeScope) or scope.broker != "IBKR" or scope.environment.value != "paper":
            raise HTTPException(status_code=409, detail="trusted IBKR PAPER account is not configured")
        try:
            return {"draft": load_paper_order_draft(runtime_root=get_runtime_root(), account_ref=scope.account_ref)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail="saved PAPER draft or its backtest evidence is unavailable") from exc

    @app.put("/api/execution/paper/order-draft")
    async def put_paper_order_draft(request: Request, body: PaperOrderPreviewRequest,
                                    _principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        scope = getattr(request.app.state, "paper_execution_scope", None)
        if not isinstance(scope, PaperRuntimeScope) or scope.broker != "IBKR" or scope.environment.value != "paper":
            raise HTTPException(status_code=409, detail="trusted IBKR PAPER account is not configured")
        if body.setup is None:
            raise HTTPException(status_code=422, detail="entry window and risk limits are required")
        try:
            draft = save_paper_order_draft(runtime_root=get_runtime_root(), account_ref=scope.account_ref,
                                           run_id=body.run_id, version_id=body.strategy_version_id,
                                           order=body.order, setup=body.setup)
            return {"draft": draft}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="PAPER draft could not be saved") from exc

    @app.post("/api/execution/paper/connect")
    async def connect_local_paper_gateway(request: Request, principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        """Discover a single local PAPER account with a read-only connection."""
        if not isinstance(principal, Principal) or not principal.paper_operator:
            raise HTTPException(status_code=403, detail="a PAPER operator is required")
        try:
            detected = await asyncio.to_thread(discover_local_paper_gateway)
        except PaperGatewayDiscoveryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        manager = _manager(request.app)
        scope = PaperRuntimeScope("IBKR", detected.account, "paper")
        try:
            with manager._lock:
                if manager is not _manager(request.app):
                    raise PaperExecutionManagerError("PAPER composition changed during discovery")
                current_scope = getattr(request.app.state, "paper_execution_scope", None)
                if current_scope is not None and current_scope != scope:
                    raise PaperExecutionManagerError("the configured PAPER account differs from the detected account")
                if manager.status() is not None:
                    if current_scope != scope:
                        raise PaperExecutionManagerError("active PAPER session has a different account")
                    return {"verified": True, "environment": "paper", "account_ref": detected.account,
                            "connection": "Gateway" if detected.port == 4002 else "TWS", "read_only_check": True}
                from dataclasses import replace
                from src.execution.order_ledger import SQLiteOrderLedger
                from src.trading.connectors.ibkr.local import load_config, save_config

                previous = load_config()
                if (manager.runtime_factory is not None and
                        (previous.profile != "paper" or previous.readonly is not True
                         or previous.host != detected.host or previous.port != detected.port
                         or previous.account != detected.account)):
                    raise PaperExecutionManagerError("redeploy before changing the PAPER Gateway endpoint")
                configured = replace(previous, profile="paper", host=detected.host, port=detected.port,
                                     account=detected.account, readonly=True)
                if manager.runtime_factory is None:
                    database = get_runtime_root() / "paper-integration-test" / "execution.sqlite"
                    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    manager.bind_stopped_activity(scope, SQLiteOrderLedger(database))
                save_config(configured)
                manager._configuration_generation += 1
                _set_paper_execution_composition(manager, _readiness(request.app), app=request.app, scope=scope)
        except Exception as exc:
            raise _management_error(exc) from exc
        return {"verified": True, "environment": "paper", "account_ref": detected.account,
                "connection": "Gateway" if detected.port == 4002 else "TWS", "read_only_check": True}

    def _deploy_paper_order_draft(request: Request, principal: Principal) -> dict[str, Any]:
        """Install a host-built draft composition without starting a broker session."""
        if not isinstance(principal, Principal) or not principal.paper_operator:
            raise HTTPException(status_code=403, detail="an attributable operator is required")
        app = request.app
        scope = getattr(app.state, "paper_execution_scope", None)
        factory = getattr(app.state, "paper_draft_composition_factory", None)
        if not isinstance(scope, PaperRuntimeScope) or not callable(factory):
            raise HTTPException(status_code=409, detail="PAPER draft deployment is not configured")
        manager = _manager(app)
        old_composition = getattr(app.state, "paper_execution_composition", None)
        old_runner = (getattr(old_composition, "draft_runner", None)
                      or getattr(old_composition, "integration_test_runner", None))
        runner_lock = getattr(old_runner, "_lock", None)
        if old_runner is not None and not callable(getattr(runner_lock, "acquire", None)):
            raise HTTPException(status_code=409, detail="current PAPER action cannot be safely reconfigured")
        if runner_lock is not None and not runner_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a PAPER action is in progress")
        try:
            with manager._lock:
                if manager is not _manager(app) or manager.status() is not None:
                    raise HTTPException(status_code=409, detail="stop the current PAPER session before deployment")
                generation = manager._lifecycle_generation
                configuration_generation = manager._configuration_generation
                stopped = getattr(manager, "_stopped_activity", None)
                if stopped is None and old_composition is not None:
                    raise HTTPException(status_code=409, detail="current PAPER order history is unavailable")
                if stopped is not None:
                    old_scope, _ledger = stopped
                    if old_scope != scope:
                        raise HTTPException(status_code=409, detail="current PAPER account changed")
            try:
                # Replay can take minutes. Build outside the event loop and
                # manager lock, then recheck all mutable state before install.
                draft = load_paper_order_draft(runtime_root=get_runtime_root(), account_ref=scope.account_ref)
                if draft is None:
                    raise ValueError("no saved PAPER order draft")
                replacement = factory(scope, principal)
                if not isinstance(replacement, PaperExecutionComposition) or replacement.scope != scope:
                    raise ValueError("draft composition scope differs")
                factory_version = getattr(replacement.manager.runtime_factory, "strategy_version_id", None)
                builder = getattr(replacement.manager.runtime_factory, "session_builder", None)
                provider = getattr(builder, "dispatch_risk_facts", None)
                policy = getattr(provider, "order_policy", None)
                grant = getattr(provider, "risk_grant", None)
                setup = PaperOrderSetup.model_validate(draft["setup"])
                order = ProposedOrder.model_validate(draft["order"])
                contract = getattr(provider, "contracts", {}).get(order.instrument_id)
                if (factory_version != draft["strategy_version_id"] or policy is None or grant is None
                        or policy.account_ref != scope.account_ref
                        or policy.strategy_version_hash != draft["strategy_version_id"] or policy.order != order
                        or contract is None or contract.con_id != draft["contract_id"]
                        or grant.account_ref != scope.account_ref
                        or grant.strategy_version_hash != draft["strategy_version_id"]
                        or grant.environment != scope.environment or grant.broker != scope.broker
                        or grant.allowed_instruments != (order.instrument_id,)
                        or grant.allowed_order_types != (order.order_type,)
                        or grant.max_order_notional != setup.max_order_notional
                        or grant.max_gross_exposure != setup.max_gross_exposure
                        or grant.strategy_budget != setup.strategy_budget
                        or grant.max_daily_loss != setup.max_daily_loss
                        or grant.max_orders_per_session != setup.max_orders_per_session
                        or grant.valid_from != setup.valid_from or grant.valid_until != setup.valid_until):
                    raise ValueError("draft composition differs from saved order or limits")
                with manager._lock:
                    if (manager is not _manager(app) or manager.status() is not None
                            or manager._lifecycle_generation != generation
                            or manager._configuration_generation != configuration_generation
                            or getattr(app.state, "paper_execution_scope", None) != scope
                            or getattr(app.state, "paper_execution_composition", None) is not old_composition):
                        raise ValueError("PAPER runtime changed during deployment")
                    if load_paper_order_draft(runtime_root=get_runtime_root(), account_ref=scope.account_ref) != draft:
                        raise ValueError("PAPER draft changed during deployment")
                    stopped_now = getattr(manager, "_stopped_activity", None)
                    if stopped_now != stopped:
                        raise ValueError("PAPER order history changed during deployment")
                    new_stopped = getattr(replacement.manager, "_stopped_activity", None)
                    if new_stopped is None or new_stopped[0] != scope:
                        raise ValueError("draft composition has no PAPER history reader")
                    if stopped is not None and Path(new_stopped[1].path).resolve() != Path(stopped[1].path).resolve():
                        raise ValueError("draft composition must preserve the existing PAPER ledger")
                    ledger = new_stopped[1]
                    recovery = ledger.recovery_snapshot_for_scope(
                        broker=scope.broker, account_ref=scope.account_ref, environment=scope.environment,
                    )
                    if any((recovery.ready_to_reserve, recovery.ready_to_send, recovery.unresolved_sends)):
                        raise ValueError("unresolved PAPER orders prevent deployment recovery")
                    deployment_id = getattr(builder, "deployment_id", None)
                    if recovery.active_orders and not isinstance(deployment_id, str):
                        raise ValueError("working orders require an exact PAPER deployment")
                    for active_order in recovery.active_orders:
                        intent = ledger.get_intent(active_order.intent_id)
                        if intent.deployment_id != deployment_id or intent.strategy_version_hash != factory_version:
                            raise ValueError("working orders belong to another PAPER deployment")
                    _set_paper_execution_composition(replacement.manager, replacement.readiness, app=app, scope=scope)
                    app.state.paper_execution_composition = replacement
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(status_code=409, detail="PAPER draft deployment could not be installed") from exc
            return {"strategy_version_id": draft["strategy_version_id"], "run_id": draft["run_id"],
                    "order_submission_performed": False, "session_started": False,
                    "matching_working_orders": len(recovery.active_orders)}
        finally:
            if runner_lock is not None:
                runner_lock.release()

    @app.post("/api/execution/paper/order-draft/deploy")
    async def deploy_paper_order_draft(request: Request, principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        return await asyncio.to_thread(_deploy_paper_order_draft, request, principal)

    @app.post("/api/execution/paper/order-draft/run")
    async def run_paper_order_draft(request: Request, principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        """Start the installed draft and enqueue its verified initial signal bar."""
        composition = getattr(request.app.state, "paper_execution_composition", None)
        runner = getattr(composition, "draft_runner", None)
        if not callable(runner):
            raise HTTPException(status_code=409, detail="deploy a PAPER draft before starting it")
        try:
            return await asyncio.to_thread(
                runner, principal,
                is_current=lambda: (getattr(request.app.state, "paper_execution_composition", None) is composition
                                    and _manager(request.app) is composition.manager),
            )
        except (PaperRuntimeAuthorizationError, PaperExecutionManagerError) as exc:
            raise _management_error(exc) from exc
        except PaperDraftRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="PAPER draft could not be started") from exc

    @app.post("/api/execution/paper/strategy-candidates/select")
    async def select_paper_strategy_candidate(
        request: Request,
        body: PaperStrategySelectRequest,
        principal: Principal = Depends(require_auth),
    ) -> dict[str, Any]:
        """Select a verified candidate without connecting to IBKR or placing an order."""
        if not isinstance(principal, Principal) or not principal.attributable:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="an attributable local operator is required to select a PAPER strategy",
            )
        manager = _manager(request.app)
        composition = getattr(request.app.state, "paper_execution_composition", None)
        runner = getattr(composition, "integration_test_runner", None)
        runner_lock = getattr(runner, "_lock", None)
        manager_lock = getattr(manager, "_lock", None)
        if not callable(getattr(runner_lock, "acquire", None)) or not callable(
            getattr(manager_lock, "acquire", None)
        ):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="the installed PAPER composition cannot be safely reconfigured",
            )
        if not runner_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="a PAPER integration action is in progress",
            )
        receipt = None
        old_composition = composition
        try:
            with manager_lock:
                if manager is not _manager(request.app):
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail="PAPER composition changed; refresh the candidate list",
                    )
                if manager.status() is not None:
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail="stop the PAPER runtime before changing the selected strategy",
                    )
                if not paper_execution_ledger_is_empty():
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail="the one-shot PAPER ledger is not empty; strategy replacement is disabled",
                    )
                try:
                    receipt = prepare_paper_strategy_selection(
                        body.strategy_version_id,
                        operator_subject=principal.subject,
                        run_id=body.run_id,
                    )
                    from src.execution.paper_integration_test_composition import (
                        build_paper_integration_test_composition,
                    )

                    replacement = build_paper_integration_test_composition()
                    if getattr(replacement.manager.runtime_factory, "strategy_version_id", None) != body.strategy_version_id:
                        raise RuntimeError("replacement composition did not pin the requested version")
                    _set_paper_execution_composition(
                        replacement.manager,
                        replacement.readiness,
                        app=request.app,
                        scope=replacement.scope,
                    )
                    request.app.state.paper_execution_composition = replacement
                    commit_paper_strategy_selection(receipt)
                except Exception as exc:
                    if receipt is not None:
                        try:
                            rollback_paper_strategy_selection(receipt)
                        except Exception:
                            pass
                    if request.app.state.paper_execution_manager is not getattr(old_composition, "manager", None):
                        try:
                            _set_paper_execution_composition(
                                old_composition.manager,
                                old_composition.readiness,
                                app=request.app,
                                scope=old_composition.scope,
                            )
                            request.app.state.paper_execution_composition = old_composition
                        except Exception:
                            pass
                    raise HTTPException(
                        status_code=http_status.HTTP_409_CONFLICT,
                        detail="PAPER strategy selection failed closed; current deployment was preserved",
                    ) from exc
            readiness = _readiness(request.app)
            return {
                "strategy_version_id": body.strategy_version_id,
                "backtest_report_sha256": receipt["report_sha256"],
                "composition_ready": readiness.composition_ready,
                "deployment_mode": "fixed_one_shot_paper_integration",
                "order_submission_performed": False,
            }
        except PaperStrategyDeploymentError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        finally:
            runner_lock.release()

    @app.get(
        "/api/execution/paper/activity/events",
        dependencies=[Depends(require_event_stream_auth)],
    )
    async def stream_paper_execution_activity(
        request: Request,
        after_seq: int | None = Query(default=None, ge=0),
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID", ge=0),
        last_event_id_query: int | None = Query(default=None, alias="Last-Event-ID", ge=0),
    ) -> StreamingResponse:
        """Replay scoped durable activity then pull from SQLite for new rows."""
        manager = _manager(request.app)
        if last_event_id is not None:
            cursor = last_event_id
        elif last_event_id_query is not None:
            cursor = last_event_id_query
        elif after_seq is not None:
            cursor = after_seq
        else:
            try:
                # A first connection starts at the current tail. Clients that
                # have a REST snapshot cursor pass it as after_seq; reconnects
                # carry Last-Event-ID automatically.
                cursor = manager.latest_activity_seq()
            except Exception as exc:
                raise _management_error(exc) from exc
        try:
            # Validate the cursor before response headers are committed.
            first_batch = manager.activity_events_since(after_seq=cursor, limit=100)
        except InvalidActivityCursor as exc:
            raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="activity cursor is ahead of the durable stream") from exc
        except Exception as exc:
            raise _management_error(exc) from exc

        async def event_stream():
            nonlocal cursor, first_batch
            batch = first_batch
            heartbeat_elapsed = 0
            while not await request.is_disconnected():
                if batch:
                    for event in batch:
                        seq = event["seq"]
                        if seq != cursor + 1:
                            # A missing durable sequence indicates corruption;
                            # force client snapshot recovery instead of claiming
                            # a synchronized stream.
                            yield "event: activity.gap\ndata: " + json.dumps(
                                {"expected_seq": cursor + 1, "actual_seq": seq},
                                separators=(",", ":"),
                            ) + "\n\n"
                            return
                        cursor = seq
                        yield (
                            f"id: {seq}\nevent: {event['event_type']}\ndata: "
                            f"{json.dumps(event['payload'], ensure_ascii=False, separators=(',', ':'))}\n\n"
                        )
                    batch = []
                    heartbeat_elapsed = 0
                    continue
                try:
                    batch = manager.activity_events_since(after_seq=cursor, limit=100)
                except Exception:
                    # The ledger remains authoritative. End the stream and let
                    # EventSource reconnect with the last delivered sequence.
                    return
                if not batch:
                    await asyncio.sleep(1.0)
                    heartbeat_elapsed += 1
                    if heartbeat_elapsed >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat_elapsed = 0

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/execution/paper/start",
        response_model=PaperExecutionStatusResponse,
    )
    async def start_paper_execution(
        request: Request,
        body: PaperStartRequest | None = Body(default=None),
        principal: Principal = Depends(require_auth),
    ) -> PaperExecutionStatusResponse:
        """Start only the scope installed by the trusted app composition root."""
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        scope = getattr(request.app.state, "paper_execution_scope", None)
        if not isinstance(scope, PaperRuntimeScope):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="trusted PAPER runtime scope is not configured",
            )
        pinned_version_id = getattr(manager.runtime_factory, "strategy_version_id", None)
        if not isinstance(pinned_version_id, str) or not is_sha256(pinned_version_id):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="trusted PAPER composition has no valid pinned strategy version",
            )
        if body is None or body.strategy_version_id != pinned_version_id:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="requested strategy version does not match the trusted PAPER composition",
            )
        try:
            # Coordinate with the strategy selection route so a request that
            # captured the previous manager cannot start it after a rebind.
            with getattr(manager, "_lock", nullcontext()):
                if manager is not _manager(request.app):
                    raise PaperExecutionManagerError("PAPER composition changed before startup")
                manager.start_for_principal(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

    @app.post("/api/execution/paper/cancel-recovery/start", response_model=PaperExecutionStatusResponse)
    async def start_paper_cancel_recovery(
        request: Request, principal: Principal = Depends(require_auth),
    ) -> PaperExecutionStatusResponse:
        """Reconnect the pinned deployment PAUSED to manage its known working orders."""
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        scope = getattr(request.app.state, "paper_execution_scope", None)
        if not isinstance(scope, PaperRuntimeScope):
            raise HTTPException(status_code=409, detail="trusted PAPER runtime scope is not configured")
        try:
            with manager._lock:
                if manager is not _manager(request.app):
                    raise PaperExecutionManagerError("PAPER composition changed before recovery")
                manager.start_for_cancel_recovery(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

    @app.post("/api/execution/paper/integration-test/run")
    async def run_paper_integration_test(
        request: Request,
        principal: Principal = Depends(require_auth),
    ) -> dict[str, Any]:
        """Fetch and feed the pinned completed bar through one authorized PAPER action."""
        composition = getattr(request.app.state, "paper_execution_composition", None)
        runner = getattr(composition, "integration_test_runner", None)
        if not callable(runner):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="one-shot PAPER integration runner is not configured",
            )
        try:
            return await asyncio.to_thread(runner, principal)
        except PaperSignalSessionMismatch as exc:
            raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail={
                "code": "paper_signal_session_mismatch",
                "message": "The backtest evaluation date is not the latest completed IBKR session.",
            }) from exc
        except PaperRuntimeAuthorizationError as exc:
            raise _management_error(exc) from exc
        except PaperExecutionManagerError as exc:
            raise _management_error(exc) from exc
        except Exception as exc:
            # Provider, Gateway and bar-data details are kept out of the HTTP
            # response; diagnostics remain in the local app/runtime logs.
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="one-shot PAPER integration action failed closed",
            ) from exc

    @app.get("/api/execution/paper/integration-test/status")
    async def get_paper_integration_test_status(
        request: Request,
        _principal: Principal = Depends(require_auth),
    ) -> dict[str, Any]:
        """Read the fixed one-shot PAPER order and its configured UTC window."""
        composition = getattr(request.app.state, "paper_execution_composition", None)
        runner = getattr(composition, "integration_test_runner", None)
        status_reader = getattr(runner, "status_snapshot", None)
        if not callable(status_reader):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="one-shot PAPER integration runner is not configured",
            )
        try:
            return status_reader()
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="one-shot PAPER integration configuration is unavailable",
            ) from exc

    @app.post(
        "/api/execution/paper/recover",
        response_model=PaperExecutionStatusResponse,
    )
    async def recover_paper_execution(
        request: Request,
        principal: Principal = Depends(require_auth),
    ) -> PaperExecutionStatusResponse:
        """Replace a failed strategy session under an independent human grant."""
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        scope = _active_scope(manager)
        try:
            manager.recover_failed_session(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

    @app.post(
        "/api/execution/paper/activate",
        response_model=PaperExecutionStatusResponse,
    )
    async def activate_paper_execution(
        request: Request,
        principal: Principal = Depends(require_auth),
    ) -> PaperExecutionStatusResponse:
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        scope = _active_scope(manager)
        composition = getattr(request.app.state, "paper_execution_composition", None)
        if getattr(composition, "manager", None) is manager and getattr(composition, "draft_runner", None) is not None:
            raise HTTPException(status_code=409, detail={"code": "paper_draft_requires_restart",
                "message": "Stop the paused draft session and prepare a fresh backtest before starting again"})
        try:
            manager.activate(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

    @app.post(
        "/api/execution/paper/cancel-owned-order",
        response_model=PaperCancelOrderResponse,
    )
    async def cancel_owned_paper_order(
        request: Request,
        body: PaperCancelOrderRequest,
        principal: Principal = Depends(require_auth),
    ) -> PaperCancelOrderResponse:
        manager = _manager(request.app)
        scope = _active_scope(manager)
        try:
            order = manager.cancel_owned_order(scope, body.intent_id, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return PaperCancelOrderResponse(intent_id=order.intent_id, state=order.state.value)

    @app.post(
        "/api/execution/paper/stop",
        response_model=PaperExecutionStatusResponse,
    )
    async def stop_paper_execution(
        request: Request,
        principal: Principal = Depends(require_auth),
    ) -> PaperExecutionStatusResponse:
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        scope = _active_scope(manager)
        try:
            manager.stop(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

    @app.post(
        "/api/execution/paper/pause",
        response_model=PaperExecutionStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def pause_paper_execution(request: Request) -> PaperExecutionStatusResponse:
        """Close the durable send gate without cancelling orders or stopping callbacks.

        Pause only reduces risk, so it is available to an authenticated API
        principal even when the current auth mechanism cannot attribute a
        human. Scope is resolved from the trusted active manager entry; the
        request accepts no account, order, reason, or IPC fields.
        """
        manager = _manager(request.app)
        readiness = _readiness(request.app)
        active = manager.status()
        if active is None:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="no managed PAPER runtime is active",
            )
        try:
            manager.pause(active.scope, reason="authenticated_operator_pause")
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="PAPER runtime did not confirm a durable pause",
            ) from exc
        return _status_response(manager, readiness)


__all__ = ["PaperExecutionStatusResponse", "register_paper_execution_routes"]
