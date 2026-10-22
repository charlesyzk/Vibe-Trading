"""Trusted process root for paper execution runtime lifecycles.

The manager owns at most one configured PAPER runtime in this V1 application.
Concrete IBKR/Backtrader composition is injected so the manager cannot invent
account, gateway, contract, authorization, or risk settings. Runtime startup
must connect, recover, and return PAUSED; only the explicit activation path may
open the durable send gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from src.execution.contracts import Environment
from src.execution.execution_service import (
    ExecutionServiceNotReady,
    PaperExecutionService,
)
from src.execution.order_ledger import ExecutionControlState
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.session.models import Principal


class PaperExecutionManagerError(RuntimeError):
    """Base error for manager lifecycle and configuration refusals."""


class PaperRuntimeUnavailable(PaperExecutionManagerError):
    """No concrete trusted runtime composition is configured."""


class PaperRuntimeConflict(PaperExecutionManagerError):
    """A runtime already owns the V1 paper account slot."""


class PaperRuntimeAuthorizationError(PaperExecutionManagerError):
    """The actor cannot be attributed or lacks the requested control grant."""


class PaperRuntimeNotReady(PaperExecutionManagerError):
    """The installed app composition lacks required runtime capabilities."""


@dataclass(frozen=True, slots=True)
class PaperRuntimeScope:
    broker: str
    account_ref: str
    environment: Environment = Environment.PAPER

    def __post_init__(self) -> None:
        if not self.broker or not self.account_ref:
            raise ValueError("broker and account_ref are required")
        if self.broker != "IBKR":
            raise ValueError("V1 paper execution manager supports the IBKR broker only")
        environment = Environment(self.environment)
        if environment is not Environment.PAPER:
            raise ValueError("this manager accepts PAPER scopes only")
        object.__setattr__(self, "environment", environment)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.broker, self.account_ref, self.environment.value


class ManagedPaperRuntime(Protocol):
    """App-level composition managed for exactly one PAPER scope."""

    service: PaperExecutionService
    durable_database_paths: tuple[str, ...]

    def start_paused(self) -> None:
        """Start SDK/callback service, reconcile broker state, and remain PAUSED."""

    def activate(self) -> None:
        """Run fresh trusted readiness checks and explicitly activate dispatch."""

    def recover_failed_session(self) -> None:
        """Replace a failed strategy session and return with dispatch PAUSED."""

    def pause(self, *, reason: str) -> None:
        """Block new sends while keeping callback/reconciliation management alive."""

    def cancel_owned_order(self, intent_id: str):
        """Request cancellation for one exact, already-owned PAPER intent."""

    def stop(self) -> bool:
        """Pause, reconcile/cancel owned work, and report whether shutdown is safe."""


@dataclass(frozen=True, slots=True)
class PaperRuntimeStatus:
    scope: PaperRuntimeScope
    phase: str
    control_state: ExecutionControlState
    reason: str | None
    owner_lease_held: bool
    last_error_code: str | None = None


@dataclass(slots=True)
class _ManagedEntry:
    scope: PaperRuntimeScope
    runtime: ManagedPaperRuntime | None
    phase: str
    last_error_code: str | None = None


RuntimeFactory = Callable[[PaperRuntimeScope], ManagedPaperRuntime]
ControlAuthorizer = Callable[[Principal, PaperRuntimeScope, str], bool]


class PaperExecutionManager:
    """Serialize startup/control of the single V1 IBKR PAPER runtime.

    The manager itself never makes a network connection. Its factory must
    return the trusted app composition, which owns the SDK connection, durable
    ledger and callback consumer. With no factory configured, starts fail
    closed. The manager retains failed/uncertain runtimes in its slot until a
    safe stop completes, preventing an in-process second owner.
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory | None,
        control_authorizer: ControlAuthorizer | None = None,
        startup_readiness: PaperExecutionReadiness | None = None,
    ) -> None:
        if runtime_factory is not None and not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable or None")
        if control_authorizer is not None and not callable(control_authorizer):
            raise TypeError("control_authorizer must be callable or None")
        if startup_readiness is not None and not isinstance(startup_readiness, PaperExecutionReadiness):
            raise TypeError("startup_readiness must be a PaperExecutionReadiness or None")
        self.runtime_factory = runtime_factory
        self.control_authorizer = control_authorizer or (lambda *_: False)
        # Missing readiness is intentionally incomplete. A bare registered
        # runtime factory cannot establish account/session/provider wiring.
        self.startup_readiness = startup_readiness or PaperExecutionReadiness()
        self._entry: _ManagedEntry | None = None
        self._lock = RLock()

    def bind_startup_readiness(self, readiness: PaperExecutionReadiness) -> None:
        """Bind the app's readiness declaration before any runtime is started."""
        if not isinstance(readiness, PaperExecutionReadiness):
            raise TypeError("readiness must be a PaperExecutionReadiness")
        with self._lock:
            if self._entry is not None:
                raise PaperRuntimeConflict("startup readiness cannot change while a runtime occupies the slot")
            self.startup_readiness = readiness

    def status(self) -> PaperRuntimeStatus | None:
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            if entry.runtime is None:
                return PaperRuntimeStatus(
                    scope=entry.scope,
                    phase=entry.phase,
                    control_state=ExecutionControlState.ERROR,
                    reason="runtime_composition_failed",
                    owner_lease_held=False,
                    last_error_code=entry.last_error_code,
                )
            state, reason = entry.runtime.service.state()
            session_error_code = getattr(entry.runtime, "health_error_code", None)
            return PaperRuntimeStatus(
                scope=entry.scope,
                phase=entry.phase,
                control_state=state,
                reason=reason,
                owner_lease_held=entry.runtime.service.lease_held,
                last_error_code=entry.last_error_code or session_error_code,
            )

    def activity_snapshot(self, *, limit: int = 50) -> dict:
        """Read durable PAPER activity through the currently trusted scope.

        The caller cannot choose broker/account/environment. Position facts are
        included only when this runtime can return its current, reconciled
        broker snapshot; ledger fills are never presented as broker positions.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self._lock:
            entry = self._entry
            if entry is None or entry.runtime is None:
                raise PaperRuntimeUnavailable("no managed PAPER runtime is active")
            runtime = entry.runtime
            scope = entry.scope
            ledger = getattr(runtime.service, "ledger", None)
            if ledger is None or not callable(getattr(ledger, "list_activity_for_scope", None)):
                raise PaperRuntimeNotReady("managed PAPER runtime has no durable activity reader")

            orders = ledger.list_activity_for_scope(
                broker=scope.broker,
                account_ref=scope.account_ref,
                environment=scope.environment,
                limit=limit,
            )
            latest_activity_seq = ledger.latest_activity_seq_for_scope(
                broker=scope.broker,
                account_ref=scope.account_ref,
                environment=scope.environment,
            )
            recovery = ledger.recovery_snapshot_for_scope(
                broker=scope.broker,
                account_ref=scope.account_ref,
                environment=scope.environment,
            )
            queue_records = (
                *recovery.ready_to_reserve,
                *recovery.ready_to_send,
                *recovery.unresolved_sends,
                *recovery.active_orders,
            )
            # A record may appear in more than one recovery queue, so aggregate
            # reservations once per intent.
            records_by_id = {record.intent_id: record for record in queue_records}
            cash_reserved = sum(
                (record.reserved_amount for record in records_by_id.values()), start=0
            )
            sell_shares_reserved = sum(
                record.remaining_qty
                for record in records_by_id.values()
                if ledger.get_intent(record.intent_id).side == "SELL"
            )
            control_state, control_reason = runtime.service.state()

            position_state = {
                "source": "unknown",
                "as_of": None,
                "positions": [],
                "reason": "no fresh authoritative broker snapshot is available",
            }
            snapshot_reader = getattr(runtime, "latest_reconciled_snapshot", None)
            if callable(snapshot_reader):
                try:
                    snapshot = snapshot_reader()
                    positions = snapshot.get("positions")
                    completed_at = snapshot.get("completed_at_utc")
                    if not isinstance(positions, list) or not isinstance(completed_at, str):
                        raise ValueError("reconciled snapshot lacks position facts or completion time")
                    position_state = {
                        "source": "ibkr_reconciled_snapshot",
                        "as_of": completed_at,
                        "positions": [
                            {
                                "instrument_id": row["symbol"],
                                "quantity": row["quantity"],
                                "average_cost": row["average_cost"],
                                "currency": row["currency"],
                            }
                            for row in positions
                        ],
                        "reason": None,
                    }
                except Exception:
                    # A stale, unavailable, or malformed snapshot is never
                    # replaced with a fill-derived approximation.
                    pass

            return {
                "broker": scope.broker,
                "environment": scope.environment.value,
                "control_state": control_state.value,
                "control_reason": control_reason,
                "orders": orders,
                "history_limit": limit,
                "latest_activity_seq": latest_activity_seq,
                "recovery": {
                    "ready_to_reserve": len(recovery.ready_to_reserve),
                    "ready_to_send": len(recovery.ready_to_send),
                    "unresolved_sends": len(recovery.unresolved_sends),
                    "active_orders": len(recovery.active_orders),
                },
                "risk": {
                    "source": "ledger_reservations_only",
                    "reserved_cash": str(cash_reserved),
                    "reserved_sell_shares": sell_shares_reserved,
                    "risk_limits": "unavailable",
                },
                "positions": position_state,
            }

    def activity_events_since(self, *, after_seq: int, limit: int = 100) -> list[dict]:
        """Read durable events only for the manager's currently trusted scope."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("activity limit must be an integer between 1 and 100")
        with self._lock:
            entry = self._entry
            if entry is None or entry.runtime is None:
                raise PaperRuntimeUnavailable("no managed PAPER runtime is active")
            ledger = getattr(entry.runtime.service, "ledger", None)
            if ledger is None or not callable(getattr(ledger, "read_activity_events_for_scope", None)):
                raise PaperRuntimeNotReady("managed PAPER runtime has no durable activity event reader")
            return ledger.read_activity_events_for_scope(
                broker=entry.scope.broker,
                account_ref=entry.scope.account_ref,
                environment=entry.scope.environment,
                after_seq=after_seq,
                limit=limit,
            )

    def latest_activity_seq(self) -> int:
        """Return the durable high-water mark for the currently trusted scope."""
        with self._lock:
            entry = self._entry
            if entry is None or entry.runtime is None:
                raise PaperRuntimeUnavailable("no managed PAPER runtime is active")
            ledger = getattr(entry.runtime.service, "ledger", None)
            if ledger is None or not callable(getattr(ledger, "latest_activity_seq_for_scope", None)):
                raise PaperRuntimeNotReady("managed PAPER runtime has no durable activity event reader")
            return ledger.latest_activity_seq_for_scope(
                broker=entry.scope.broker,
                account_ref=entry.scope.account_ref,
                environment=entry.scope.environment,
            )

    def start(self, scope: PaperRuntimeScope) -> PaperRuntimeStatus:
        """Start one runtime and require recovery to finish in PAUSED state."""
        if self.runtime_factory is None:
            raise PaperRuntimeUnavailable("paper runtime composition is not configured")
        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a PaperRuntimeScope")
        with self._lock:
            if self._entry is not None:
                if self._entry.scope.key == scope.key and self._entry.phase == "READY":
                    status = self.status()
                    assert status is not None
                    return status
                raise PaperRuntimeConflict("the V1 paper runtime slot is already occupied")

            if not self.startup_readiness.composition_ready:
                missing = ",".join(self.startup_readiness.missing_requirements)
                raise PaperRuntimeNotReady(f"PAPER runtime composition is incomplete: {missing}")

            entry = _ManagedEntry(scope=scope, runtime=None, phase="STARTING")
            self._entry = entry
            started = False
            try:
                runtime = self.runtime_factory(scope)
                entry.runtime = runtime
                self._validate_runtime_scope(runtime, scope)
                started = True
                runtime.start_paused()
                state, _reason = runtime.service.state()
                if state is not ExecutionControlState.PAUSED or not runtime.service.lease_held:
                    raise ExecutionServiceNotReady(
                        "runtime startup must finish recovered, PAUSED, and holding the owner lease"
                    )
                entry.phase = "READY"
                return self.status()  # type: ignore[return-value]
            except Exception as exc:
                entry.phase = "START_FAILED"
                entry.last_error_code = type(exc).__name__
                if entry.runtime is None or not started:
                    # Factory construction failed before any runtime was
                    # returned, so no owned process or lease can be released.
                    self._entry = None
                raise PaperExecutionManagerError(
                    "paper runtime did not complete safe paused startup"
                ) from exc

    def start_for_principal(self, scope: PaperRuntimeScope, principal: Principal) -> PaperRuntimeStatus:
        """Start a runtime only after an attributable human's explicit grant.

        Startup connects and reads/reconciles broker state even though it must
        remain PAUSED. Keep the authorizer check in the same serialized manager
        critical section as the start transition so an API caller cannot turn
        a shared-key authentication result into a broker connection.
        """
        with self._lock:
            self._authorize(principal, scope, "start")
            return self.start(scope)

    def activate(self, scope: PaperRuntimeScope, principal: Principal) -> PaperRuntimeStatus:
        """Open dispatch only for an attributable actor with explicit grant."""
        with self._lock:
            entry = self._require_entry(scope, phase="READY")
            self._authorize(principal, scope, "activate")
            assert entry.runtime is not None
            entry.runtime.activate()
            state, _reason = entry.runtime.service.state()
            if state is not ExecutionControlState.RUNNING:
                entry.phase = "ERROR"
                raise ExecutionServiceNotReady("runtime activation did not open the durable RUNNING gate")
            return self.status()  # type: ignore[return-value]

    def recover_failed_session(
        self, scope: PaperRuntimeScope, principal: Principal,
    ) -> PaperRuntimeStatus:
        """Recover only a failed managed strategy under a distinct grant.

        Recovery keeps the existing runtime/lease, reconstructs its strategy
        shell, and must return PAUSED. It never falls through to activation.
        """
        with self._lock:
            entry = self._require_entry(scope, phase="READY")
            self._authorize(principal, scope, "recover")
            runtime = entry.runtime
            assert runtime is not None
            if getattr(runtime, "health_error_code", None) != "managed_python_session_failed":
                raise PaperRuntimeConflict("runtime has no recoverable failed strategy session")
            state, _reason = runtime.service.state()
            if state is not ExecutionControlState.PAUSED or not runtime.service.lease_held:
                raise PaperRuntimeConflict("failed-session recovery requires PAUSED state and retained lease")
            recover = getattr(runtime, "recover_failed_session", None)
            if not callable(recover):
                raise PaperRuntimeNotReady("runtime composition does not support failed-session recovery")
            entry.phase = "RECOVERING"
            try:
                recover()
                state, _reason = runtime.service.state()
                if state is not ExecutionControlState.PAUSED or not runtime.service.lease_held:
                    raise ExecutionServiceNotReady(
                        "failed-session recovery must return PAUSED and retain the owner lease"
                    )
                entry.phase = "READY"
                entry.last_error_code = None
                return self.status()  # type: ignore[return-value]
            except Exception as exc:
                entry.phase = "READY"
                entry.last_error_code = type(exc).__name__
                raise PaperExecutionManagerError(
                    "failed strategy session remains paused and unrecovered"
                ) from exc

    def retry_recovery(self, scope: PaperRuntimeScope) -> PaperRuntimeStatus:
        """Retry trusted broker reconciliation after a retained startup failure."""
        with self._lock:
            entry = self._require_entry(scope, phase="START_FAILED")
            assert entry.runtime is not None
            entry.phase = "RECOVERING"
            try:
                entry.runtime.start_paused()
                state, _reason = entry.runtime.service.state()
                if state is not ExecutionControlState.PAUSED or not entry.runtime.service.lease_held:
                    raise ExecutionServiceNotReady(
                        "runtime recovery must finish PAUSED and retain the owner lease"
                    )
                entry.phase = "READY"
                entry.last_error_code = None
                return self.status()  # type: ignore[return-value]
            except Exception as exc:
                entry.phase = "START_FAILED"
                entry.last_error_code = type(exc).__name__
                raise PaperExecutionManagerError("paper runtime recovery remains incomplete") from exc

    def pause(self, scope: PaperRuntimeScope, *, reason: str = "operator_pause") -> PaperRuntimeStatus:
        """Apply the safe, send-blocking pause operation."""
        with self._lock:
            entry = self._require_entry(scope)
            assert entry.runtime is not None
            entry.runtime.pause(reason=reason)
            state, _reason = entry.runtime.service.state()
            if state is not ExecutionControlState.PAUSED:
                entry.phase = "ERROR"
                raise ExecutionServiceNotReady("runtime failed to persist PAUSED state")
            entry.phase = "READY"
            return self.status()  # type: ignore[return-value]

    def cancel_owned_order(self, scope: PaperRuntimeScope, intent_id: str, principal: Principal):
        """Issue one independently authorized cancel request for an owned order.

        The request addresses a local intent ID. The runtime resolves the
        broker order ID from its durable account-scoped binding; callers never
        supply a broker ID or SDK command payload.
        """
        with self._lock:
            entry = self._require_entry(scope)
            if entry.phase not in {"READY", "STOPPING"}:
                raise PaperRuntimeConflict(f"runtime phase {entry.phase} does not allow cancellation")
            self._authorize(principal, scope, "cancel")
            assert entry.runtime is not None
            return entry.runtime.cancel_owned_order(intent_id)

    def stop(self, scope: PaperRuntimeScope, principal: Principal) -> bool:
        """Stop only after an attributable actor is authorized and runtime quiesces."""
        with self._lock:
            entry = self._require_entry(scope)
            self._authorize(principal, scope, "stop")
            assert entry.runtime is not None
            entry.phase = "STOPPING"
            stopped = entry.runtime.stop()
            if stopped:
                state, _reason = entry.runtime.service.state()
                if state is not ExecutionControlState.STOPPED or entry.runtime.service.lease_held:
                    entry.phase = "ERROR"
                    raise ExecutionServiceNotReady(
                        "runtime reported stopped without durable STOPPED state and released lease"
                    )
                self._entry = None
                return True
            return False

    def shutdown_active_runtime(self) -> bool:
        """Safely stop the owned runtime as part of trusted app shutdown.

        Process lifecycle shutdown is distinct from a user management action:
        it does not impersonate a principal, cancel orders, or clear positions.
        The runtime must prove quiescence and release its lease before the
        manager forgets the entry. Any failure keeps the slot visible for
        recovery/review.
        """
        with self._lock:
            entry = self._entry
            if entry is None:
                return True
            if entry.runtime is None:
                # A factory may have failed before returning an owned runtime;
                # there is no child or lease to stop.
                self._entry = None
                return True

            entry.phase = "STOPPING"
            try:
                stopped = entry.runtime.stop()
            except Exception as exc:
                entry.phase = "ERROR"
                entry.last_error_code = type(exc).__name__
                return False

            if not stopped:
                # Keep both the runtime reference and manager slot. The
                # service/runtime own the durable STOPPING state and lease.
                return False

            try:
                state, _reason = entry.runtime.service.state()
                lease_held = entry.runtime.service.lease_held
            except Exception as exc:
                entry.phase = "ERROR"
                entry.last_error_code = type(exc).__name__
                return False
            if state is not ExecutionControlState.STOPPED or lease_held:
                entry.phase = "ERROR"
                entry.last_error_code = "ShutdownInvariantError"
                return False

            self._entry = None
            return True

    def _require_entry(self, scope: PaperRuntimeScope, *, phase: str | None = None) -> _ManagedEntry:
        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a PaperRuntimeScope")
        entry = self._entry
        if entry is None or entry.scope.key != scope.key or entry.runtime is None:
            raise PaperRuntimeUnavailable("no managed paper runtime exists for this scope")
        if phase is not None and entry.phase != phase:
            raise PaperRuntimeConflict(f"runtime phase {entry.phase} does not allow this operation")
        return entry

    def _authorize(self, principal: Principal, scope: PaperRuntimeScope, action: str) -> None:
        if not isinstance(principal, Principal) or not principal.attributable:
            raise PaperRuntimeAuthorizationError("an attributable human principal is required")
        try:
            allowed = self.control_authorizer(principal, scope, action)
        except Exception as exc:
            raise PaperRuntimeAuthorizationError("paper control authorization check failed") from exc
        if allowed is not True:
            raise PaperRuntimeAuthorizationError("paper control grant does not authorize this action")

    @staticmethod
    def _validate_runtime_scope(runtime: ManagedPaperRuntime, scope: PaperRuntimeScope) -> None:
        if not isinstance(runtime.service, PaperExecutionService):
            raise TypeError("runtime must own a PaperExecutionService")
        service = runtime.service
        if (
            service.broker != scope.broker
            or service.account_ref != scope.account_ref
            or service.environment is not scope.environment
        ):
            raise PaperExecutionManagerError("runtime service scope differs from requested scope")
        try:
            canonical_ledger_path = Path(service.ledger.path).expanduser().resolve()
            runtime_paths = tuple(
                Path(path).expanduser().resolve() for path in runtime.durable_database_paths
            )
        except Exception as exc:
            raise PaperExecutionManagerError("runtime durable database paths are invalid") from exc
        if not runtime_paths or any(path != canonical_ledger_path for path in runtime_paths):
            raise PaperExecutionManagerError(
                "ledger, callback index/spool, and order-id allocator must share one canonical database"
            )
