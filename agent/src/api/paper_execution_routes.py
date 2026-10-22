"""Read-only status surface for the managed IBKR PAPER runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.security import require_auth, require_event_stream_auth
from src.api.state import _get_paper_execution_manager, _get_paper_execution_readiness
from src.execution.order_ledger import ExecutionControlState, InvalidActivityCursor
from src.execution.contracts import is_sha256
from src.execution.paper_execution_manager import (
    PaperExecutionManagerError,
    PaperRuntimeAuthorizationError,
    PaperRuntimeScope,
    PaperRuntimeStatus,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
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
            manager.start_for_principal(scope, principal)
        except Exception as exc:
            raise _management_error(exc) from exc
        return _status_response(manager, readiness)

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
