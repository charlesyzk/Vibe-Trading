"""Fail-closed lifecycle owner for a paper execution scope.

This is a host-side lifecycle primitive, not an IBKR client factory or API
manager. Callers inject recovery/readiness/quiescence checks from the eventual
trusted composition root. A scope stays PAUSED after service startup until an
explicit activation succeeds.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from src.execution.contracts import Environment
from src.execution.order_ledger import ExecutionControlState, SQLiteOrderLedger
from src.execution.owner_lease import ExecutionOwnerLease


class ExecutionServiceError(RuntimeError):
    """Base error for a refused or incomplete execution lifecycle action."""


class ExecutionServiceNotReady(ExecutionServiceError):
    """A required recovery or activation check has not completed successfully."""


class PaperExecutionService:
    """Own the local lease and durable control gate for one PAPER account.

    The checks are deliberately injected and must return ``True`` only when
    their corresponding trusted facts have been verified. Recovery is checked
    on every ``start`` call while the durable state is RECOVERING; activation
    is a separate explicit step. ``quiesce`` must confirm no send is in flight
    and all managed orders have reached a safe terminal/reconciled state before
    the owner lease is released.
    """

    def __init__(
        self,
        *,
        ledger: SQLiteOrderLedger,
        lock_directory: str | Path,
        broker: str,
        account_ref: str,
        environment: Environment,
        owner_id: str,
        recovery_check: Callable[[], bool],
        activation_check: Callable[[], bool],
        quiesce_check: Callable[[], bool],
    ) -> None:
        if Environment(environment) is not Environment.PAPER:
            raise ValueError("PaperExecutionService only accepts the PAPER environment")
        if not broker or not account_ref or not owner_id:
            raise ValueError("broker, account_ref, and owner_id are required")
        for name, check in (
            ("recovery_check", recovery_check),
            ("activation_check", activation_check),
            ("quiesce_check", quiesce_check),
        ):
            if not callable(check):
                raise TypeError(f"{name} must be callable")
        self.ledger = ledger
        self.lock_directory = Path(lock_directory)
        self.broker = broker
        self.account_ref = account_ref
        self.environment = Environment(environment)
        self.owner_id = owner_id
        self.recovery_check = recovery_check
        self.activation_check = activation_check
        self.quiesce_check = quiesce_check
        self._lease: ExecutionOwnerLease | None = None
        self._lock = threading.RLock()

    @property
    def lease_held(self) -> bool:
        lease = self._lease
        return lease is not None and lease.held

    def state(self) -> tuple[ExecutionControlState, str | None]:
        return self.ledger.get_execution_control(
            broker=self.broker,
            account_ref=self.account_ref,
            environment=self.environment,
        )

    def require_owner_lease(self) -> ExecutionOwnerLease:
        """Return the held scope lease for trusted broker composition."""
        with self._lock:
            if self._lease is None or not self._lease.held:
                raise ExecutionServiceNotReady("execution owner lease is not held")
            self._lease.assert_scope(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
            )
            return self._lease

    def start(self) -> ExecutionControlState:
        """Acquire ownership and complete recovery, leaving the gate PAUSED."""
        # A RUNNING durable gate is never evidence that this start/recovery
        # request can be skipped. begin_recovery() first closes it and records
        # RECOVERING; only a fresh successful recovery check may return PAUSED.
        self.begin_recovery()
        return self.complete_recovery()

    def begin_recovery(self) -> ExecutionControlState:
        """Acquire the local account lease and durably close the send gate.

        A concrete runtime uses this before starting the SDK child. Broker
        callbacks and snapshot reconciliation then run while state is
        RECOVERING; only ``complete_recovery`` may move the scope to PAUSED.
        """
        with self._lock:
            state, _reason = self.state()
            if state is ExecutionControlState.ERROR:
                raise ExecutionServiceNotReady(f"scope requires operator review from {state.value}")
            if self._lease is None or not self._lease.held:
                self._lease = ExecutionOwnerLease(
                    lock_directory=self.lock_directory,
                    broker=self.broker,
                    account_ref=self.account_ref,
                    environment=self.environment,
                    owner_id=self.owner_id,
                )
            self._lease.assert_scope(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
            )
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.RECOVERING,
                reason="service_start_recovery",
            )

            return ExecutionControlState.RECOVERING

    def complete_recovery(self) -> ExecutionControlState:
        """Complete trusted broker reconciliation and leave dispatch PAUSED."""
        with self._lock:
            if not self.lease_held:
                raise ExecutionServiceNotReady("execution owner lease is not held")
            self._lease.assert_scope(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
            )
            state, _reason = self.state()
            if state is not ExecutionControlState.RECOVERING:
                raise ExecutionServiceNotReady(f"cannot complete recovery from {state.value}")
            try:
                recovered = self.recovery_check()
            except Exception as exc:
                raise ExecutionServiceNotReady("paper execution recovery check failed") from exc
            if recovered is not True:
                raise ExecutionServiceNotReady("paper execution recovery is incomplete")
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.PAUSED,
                reason="recovered_requires_explicit_activation",
            )
            return ExecutionControlState.PAUSED

    def abort_recovery(self, *, reason: str = "recovery_failed") -> ExecutionControlState:
        """Return a failed, leased recovery attempt to PAUSED without opening sends."""
        if not reason or len(reason) > 1000:
            raise ValueError("reason must contain 1 to 1000 characters")
        with self._lock:
            if not self.lease_held:
                raise ExecutionServiceNotReady("execution owner lease is not held")
            state, _previous_reason = self.state()
            if state is not ExecutionControlState.RECOVERING:
                raise ExecutionServiceNotReady(f"cannot abort recovery from {state.value}")
            self._lease.assert_scope(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
            )
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.PAUSED,
                reason=reason,
            )
            return ExecutionControlState.PAUSED

    def activate(self) -> ExecutionControlState:
        """Explicitly open the durable send gate after fresh readiness checks."""
        with self._lock:
            if not self.lease_held:
                raise ExecutionServiceNotReady("execution owner lease is not held")
            self._lease.assert_scope(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
            )
            state, _reason = self.state()
            if state is not ExecutionControlState.PAUSED:
                raise ExecutionServiceNotReady(f"cannot activate scope from {state.value}")
            try:
                ready = self.activation_check()
            except Exception as exc:
                raise ExecutionServiceNotReady("paper execution readiness check failed") from exc
            if ready is not True:
                raise ExecutionServiceNotReady("paper execution readiness checks are incomplete")
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.RUNNING,
                reason="explicit_activation",
            )
            return ExecutionControlState.RUNNING

    def pause(self, *, reason: str = "operator_pause") -> ExecutionControlState:
        """Block new sends while keeping the local owner for safe management."""
        if not reason or len(reason) > 1000:
            raise ValueError("reason must contain 1 to 1000 characters")
        with self._lock:
            if not self.lease_held:
                raise ExecutionServiceNotReady("execution owner lease is not held")
            state, _previous_reason = self.state()
            if state not in {ExecutionControlState.RUNNING, ExecutionControlState.PAUSED}:
                raise ExecutionServiceNotReady(f"cannot pause scope from {state.value}")
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.PAUSED,
                reason=reason,
            )
            return ExecutionControlState.PAUSED

    def stop(self) -> bool:
        """Stop dispatch, then release ownership only after quiescence is proven.

        Returns ``False`` while the scope must remain STOPPING and retain its
        owner lease. The service can retry after callbacks/reconciliation drain.
        """
        with self._lock:
            if not self.lease_held:
                state, _reason = self.state()
                return state is ExecutionControlState.STOPPED
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.STOPPING,
                reason="awaiting_quiescence",
            )
            try:
                quiescent = self.quiesce_check()
            except Exception as exc:
                raise ExecutionServiceNotReady("paper execution quiescence check failed") from exc
            if quiescent is not True:
                return False
            self.ledger.set_execution_control(
                broker=self.broker,
                account_ref=self.account_ref,
                environment=self.environment,
                state=ExecutionControlState.STOPPED,
                reason="quiescent",
            )
            assert self._lease is not None
            self._lease.close()
            self._lease = None
            return True

    def close(self) -> None:
        """Close gracefully only after the scope proves it is quiescent.

        If quiescence cannot be proven, the durable state remains STOPPING and
        the owner lease remains held. Callers must keep the service alive and
        retry ``stop`` after callbacks/reconciliation drain.
        """
        with self._lock:
            if not self.lease_held:
                return
            if not self.stop():
                raise ExecutionServiceNotReady(
                    "execution service is not quiescent; STOPPING state and owner lease are retained"
                )

    def __enter__(self) -> "PaperExecutionService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
