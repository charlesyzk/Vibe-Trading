"""App-managed orchestration for one isolated IBKR PAPER runtime.

Construction is deliberately offline. ``start_paused`` is the explicit
connection boundary and always performs broker snapshot reconciliation before
the lifecycle service can leave RECOVERING. No API route or app lifespan calls
it implicitly.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.execution.contracts import Environment
from src.execution.execution_service import PaperExecutionService
from src.execution.ibkr_callback_pump import (
    BrokerOrderIndex,
    IbkrCallbackPump,
    IbkrCallbackPumpWorker,
)
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator
from src.execution.ibkr_reconciliation import (
    IbkrReconciliationCoordinator,
    IbkrRecoveryReconciler,
    ReconciliationSnapshotError,
    SQLiteIbkrSnapshotStore,
)
from src.execution.ibkr_runtime_process import (
    IbkrRuntimeHealthMonitor,
    IbkrRuntimeProcess,
)
from src.execution.order_ledger import (
    ExecutionControlState,
    InvalidTransition,
    OrderRecord,
    SQLiteOrderLedger,
)


class ManagedRuntimeError(RuntimeError):
    """The PAPER runtime could not reach a verified lifecycle state."""


class IbkrManagedPaperRuntime:
    """Compose SDK process, durable callback consumer, recovery and lifecycle.

    The caller supplies every account-specific setting and trusted risk/stop
    check. The class never invents a Gateway endpoint or account and does not
    connect during construction. Its safe-stop callback runs only after the
    configured quiescence check succeeds, while the service still holds its
    account lease.
    """

    def __init__(
        self,
        *,
        service: PaperExecutionService,
        process: IbkrRuntimeProcess,
        callback_pump: IbkrCallbackPump,
        callback_worker: IbkrCallbackPumpWorker,
        health_monitor: IbkrRuntimeHealthMonitor,
        coordinator: IbkrReconciliationCoordinator,
        reconciler: IbkrRecoveryReconciler,
        ledger: SQLiteOrderLedger,
        order_index: BrokerOrderIndex,
        allocator: IbkrOrderIdAllocator,
        snapshot_store: SQLiteIbkrSnapshotStore,
        gateway_host: str,
        gateway_port: int,
        client_id: int,
        account_ref: str,
        recovery_timeout: float = 30.0,
        snapshot_timeout: float = 30.0,
        stop_timeout: float = 10.0,
        poll_interval: float = 0.05,
        snapshot_id_factory: Callable[[], int] | None = None,
        snapshot_freshness_seconds: float = 10.0,
    ) -> None:
        if service.broker != "IBKR" or service.environment is not Environment.PAPER:
            raise ValueError("managed IBKR runtime requires an IBKR/PAPER lifecycle service")
        if service.account_ref != account_ref or reconciler.account_ref != account_ref:
            raise ValueError("service, snapshot coordinator and reconciler must share the configured account")
        if not isinstance(gateway_host, str) or gateway_host.strip().lower() not in {
            "localhost", "127.0.0.1", "::1",
        }:
            raise ValueError("PAPER Gateway host must be an explicit loopback address")
        if type(gateway_port) is not int or not 1 <= gateway_port <= 65535:
            raise ValueError("gateway_port must be an integer from 1 to 65535")
        if gateway_port not in {4002, 7497}:
            raise ValueError("managed PAPER runtime accepts only default IB Gateway/TWS PAPER ports")
        if type(client_id) is not int or client_id < 0:
            raise ValueError("client_id must be a non-negative integer")
        if not isinstance(account_ref, str) or not account_ref.strip():
            raise ValueError("account_ref is required")
        for name, value in (
            ("recovery_timeout", recovery_timeout),
            ("snapshot_timeout", snapshot_timeout),
            ("stop_timeout", stop_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if (
            isinstance(snapshot_freshness_seconds, bool)
            or not isinstance(snapshot_freshness_seconds, (int, float))
            or not 1 <= snapshot_freshness_seconds <= 300
        ):
            raise ValueError("snapshot_freshness_seconds must be between 1 and 300 seconds")
        if not isinstance(service, PaperExecutionService):
            raise TypeError("service must be PaperExecutionService")
        if not isinstance(process, IbkrRuntimeProcess):
            raise TypeError("process must be IbkrRuntimeProcess")
        if not isinstance(callback_pump, IbkrCallbackPump):
            raise TypeError("callback_pump must be IbkrCallbackPump")
        if not isinstance(callback_worker, IbkrCallbackPumpWorker):
            raise TypeError("callback_worker must be IbkrCallbackPumpWorker")
        if not isinstance(health_monitor, IbkrRuntimeHealthMonitor):
            raise TypeError("health_monitor must be IbkrRuntimeHealthMonitor")
        if not isinstance(coordinator, IbkrReconciliationCoordinator):
            raise TypeError("coordinator must be IbkrReconciliationCoordinator")
        if not isinstance(reconciler, IbkrRecoveryReconciler):
            raise TypeError("reconciler must be IbkrRecoveryReconciler")
        if not isinstance(ledger, SQLiteOrderLedger) or not isinstance(order_index, BrokerOrderIndex):
            raise TypeError("ledger and order_index must be trusted SQLite components")
        if not isinstance(allocator, IbkrOrderIdAllocator):
            raise TypeError("allocator must be IbkrOrderIdAllocator")
        if not isinstance(snapshot_store, SQLiteIbkrSnapshotStore):
            raise TypeError("snapshot_store must be SQLiteIbkrSnapshotStore")

        canonical_path = Path(ledger.path).expanduser().resolve()
        paths = (
            process.spool_path,
            order_index.path,
            allocator.path,
            snapshot_store.path,
            reconciler.store.path,
            reconciler.order_index.path,
            reconciler.ledger.path,
            coordinator.store.path,
            service.ledger.path,
        )
        if any(Path(path).expanduser().resolve() != canonical_path for path in paths):
            raise ValueError("all managed PAPER runtime state must share one canonical SQLite database")
        if any(Path(path).expanduser().resolve() != canonical_path for path in (
            callback_pump.ledger.path, callback_pump.order_index.path,
        )):
            raise ValueError("callback pump must use the runtime ledger and order index")
        if callback_pump.account_ref != account_ref or callback_pump.environment is not Environment.PAPER:
            raise ValueError("callback pump scope differs from the configured PAPER account")
        if callback_pump.broker != "IBKR" or callback_worker.pump is not callback_pump:
            raise ValueError("managed callback worker must consume the configured IBKR callback pump")
        if coordinator.process is not process or coordinator.store is not snapshot_store:
            raise ValueError("snapshot coordinator must use this SDK process and snapshot store")
        if coordinator.account_ref != account_ref:
            raise ValueError("snapshot coordinator account differs from the configured PAPER account")
        if (
            reconciler.allocator is not allocator or reconciler.store is not snapshot_store
            or reconciler.ledger is not ledger or reconciler.order_index is not order_index
        ):
            raise ValueError("reconciler must use this snapshot store and order-id allocator")
        if snapshot_id_factory is not None and not callable(snapshot_id_factory):
            raise TypeError("snapshot_id_factory must be callable")

        self.service = service
        self.process = process
        self.callback_pump = callback_pump
        self.callback_worker = callback_worker
        self.health_monitor = health_monitor
        self.coordinator = coordinator
        self.reconciler = reconciler
        self.ledger = ledger
        self.order_index = order_index
        self.allocator = allocator
        self.snapshot_store = snapshot_store
        self.gateway_host = gateway_host.strip()
        self.gateway_port = gateway_port
        self.client_id = client_id
        self.account_ref = account_ref
        self.recovery_timeout = float(recovery_timeout)
        self.snapshot_timeout = float(snapshot_timeout)
        self.stop_timeout = float(stop_timeout)
        self.poll_interval = float(poll_interval)
        self.snapshot_freshness_seconds = float(snapshot_freshness_seconds)
        self.snapshot_id_factory = snapshot_id_factory or (lambda: secrets.randbelow(2**62) + 1)
        # Bind the trusted callback facts to this exact runtime's durable
        # consumers, even if the caller assembled the pump independently.
        callback_pump.next_valid_id_sink = allocator.observe_next_valid_id
        callback_pump.reconciliation_sink = snapshot_store.record
        health_monitor.status_reader = lambda: process.request("status")
        self._lock = threading.RLock()
        self._started = False
        self._connected = False
        self._hydration_snapshot_available = False
        self._worker_started = False
        self._process_closed = False
        self._monitor_started = False
        self._latest_snapshot_id: int | None = None
        self._external_recovery_check = service.recovery_check
        self._external_activation_check = service.activation_check
        self._external_quiesce_check = service.quiesce_check
        # The service can only complete recovery after this runtime records a
        # durable proof and opens the allocator for the checked candidate.
        service.recovery_check = self._recovery_proof_valid
        service.activation_check = self._activation_ready
        service.quiesce_check = self._quiesce_and_shutdown
        self._external_failure_sink = callback_worker.failure_sink
        callback_worker.failure_sink = self._callback_worker_failed
        self._external_health_pause = health_monitor.pause
        health_monitor.pause = self._health_monitor_pause

    @property
    def durable_database_paths(self) -> tuple[str, ...]:
        return (self.ledger.path, self.order_index.path, self.allocator.path, self.snapshot_store.path)

    def latest_reconciled_snapshot(self) -> dict:
        """Return the latest proven PAPER snapshot while runtime remains PAUSED.

        This is the trusted handoff for pre-start Backtrader broker hydration.
        It deliberately does not expose a merely complete capture, an old
        snapshot from a different runtime epoch, or facts while dispatch is
        active. Callers must still provide an explicit conId-to-feed mapping
        to ``PortBroker.hydrate_reconciled_account_snapshot``.
        """
        with self._lock:
            state, _reason = self.service.state()
            if state is not ExecutionControlState.PAUSED or not self._hydration_snapshot_available:
                raise ManagedRuntimeError("reconciled account facts are available only while runtime is PAUSED")
            if self._latest_snapshot_id is None or not self.service.lease_held:
                raise ManagedRuntimeError("current runtime epoch has no leased reconciled snapshot")
            try:
                self._assert_sdk_healthy()
                snapshot = self.snapshot_store.complete_snapshot(self._latest_snapshot_id)
            except Exception as exc:
                raise ManagedRuntimeError("latest runtime snapshot is unavailable") from exc
            proof = snapshot.get("reconciliation_result")
            try:
                completed_at = datetime.fromisoformat(str(snapshot.get("completed_at_utc")))
                if completed_at.tzinfo is None or completed_at.utcoffset() is None:
                    raise ValueError("snapshot completion time must be timezone-aware")
                snapshot_age = (datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError) as exc:
                raise ManagedRuntimeError("latest runtime snapshot has an invalid completion timestamp") from exc
            if snapshot_age < -5 or snapshot_age > self.snapshot_freshness_seconds:
                raise ManagedRuntimeError("latest runtime snapshot exceeded its hydration freshness window")
            if (
                snapshot.get("capture_complete") is not True
                or snapshot.get("reconciled") is not True
                or snapshot.get("account_ref") != self.account_ref
                or snapshot.get("environment") != Environment.PAPER.value
                or not isinstance(proof, dict)
                or proof.get("snapshot_id") != self._latest_snapshot_id
                or proof.get("account_ref") != self.account_ref
                or proof.get("environment") != Environment.PAPER.value
                or not {"scope", "open_orders", "positions", "order_id_high_water_mark"}.issubset(
                    proof.get("checks", []) if isinstance(proof.get("checks"), list) else []
                )
                or not self._recovery_proof_valid()
            ):
                raise ManagedRuntimeError("latest runtime snapshot lacks a current matching reconciliation proof")
            return snapshot

    def start_paused(self) -> None:
        """Explicitly connect, consume callbacks, reconcile, then remain PAUSED."""
        with self._lock:
            if self._process_closed:
                raise ManagedRuntimeError("closed SDK child cannot be reused; construct a new runtime epoch")
            try:
                if not self._started:
                    self.service.begin_recovery()
                    status = self.process.start_process()
                    if status.get("state") != "NEW":
                        raise ManagedRuntimeError("SDK child did not start in the disconnected NEW state")
                    self.callback_worker.start()
                    self._worker_started = True
                    self._started = True
                    self.process.request(
                        "start", host=self.gateway_host, port=self.gateway_port, client_id=self.client_id,
                    )
                elif self.service.state()[0] is not ExecutionControlState.RECOVERING:
                    self.service.begin_recovery()

                if not self._connected:
                    self._wait_for_sdk_handshake()
                    self._connected = True
                else:
                    self._request_fresh_order_id()
                self._assert_sdk_healthy()
                snapshot_id = self.snapshot_id_factory()
                if type(snapshot_id) is not int or snapshot_id <= 0:
                    raise ManagedRuntimeError("snapshot id factory must return a positive integer")
                self._latest_snapshot_id = snapshot_id
                self.coordinator.request_snapshot(snapshot_id)
                self._wait_for_snapshot(snapshot_id)
                self.reconciler.reconcile(snapshot_id)
                self.service.complete_recovery()
                self._hydration_snapshot_available = True
                self._assert_sdk_healthy()
                if not self._monitor_started:
                    self.health_monitor.start()
                    self._monitor_started = True
            except Exception:
                try:
                    if self.service.state()[0] is ExecutionControlState.RECOVERING:
                        self.service.abort_recovery(reason="managed_runtime_recovery_failed")
                except Exception:
                    pass
                raise

    def _request_fresh_order_id(self) -> None:
        """Require a new reqIds/nextValidId fact before same-connection recovery."""
        worker = self.callback_worker
        if not worker.pause(timeout=self.stop_timeout):
            worker.resume()
            raise ManagedRuntimeError("callback worker could not quiesce before requesting nextValidId")
        try:
            # reqIds has no correlation token. Require all earlier parent-side
            # callback work to be drained before opening a fresh candidate
            # window, then wait for the pump generation to advance.
            if not self.callback_pump.events.empty() or self.order_index.pending_callbacks():
                raise ManagedRuntimeError("old broker callbacks must drain before requesting nextValidId")
            generation = self.callback_pump.next_valid_id_generation
            self.allocator.require_fresh_candidate()
        except Exception:
            worker.resume()
            raise
        try:
            request = getattr(self.process, "request_order_ids", None)
            if not callable(request):
                raise ManagedRuntimeError("SDK runtime cannot request a fresh nextValidId")
            request()
        except Exception as exc:
            worker.resume()
            raise ManagedRuntimeError("IBKR reqIds request failed during runtime recovery") from exc
        worker.resume()
        deadline = time.monotonic() + self.recovery_timeout
        while time.monotonic() < deadline:
            self._raise_worker_failure()
            self._assert_sdk_healthy()
            if self.callback_pump.next_valid_id_generation > generation:
                self.allocator.candidate_for_reconciliation()
                return
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("fresh IBKR nextValidId did not arrive after reqIds")

    def activate(self) -> None:
        """Open dispatch only after runtime health and the supplied risk check."""
        with self._lock:
            self._assert_sdk_healthy()
            if not self.allocator.allocation_ready or not self._recovery_proof_valid():
                raise ManagedRuntimeError("fresh broker reconciliation proof is required before activation")
            self.service.activate()
            self._hydration_snapshot_available = False

    def pause(self, *, reason: str) -> None:
        with self._lock:
            self.service.pause(reason=reason)

    def cancel_owned_order(self, intent_id: str) -> OrderRecord:
        """Durably request cancel without depending on the strategy thread."""
        with self._lock:
            state, _reason = self.service.state()
            if state not in {ExecutionControlState.RUNNING, ExecutionControlState.PAUSED}:
                raise ManagedRuntimeError("owned-order cancellation requires a running or paused runtime")
            if not self.service.lease_held or self._process_closed:
                raise ManagedRuntimeError("owned-order cancellation requires the active runtime lease")
            intent = self.ledger.get_intent(intent_id)
            if (
                intent.broker != "IBKR"
                or intent.account_ref != self.account_ref
                or intent.environment is not Environment.PAPER
            ):
                raise ManagedRuntimeError("intent is outside this managed IBKR PAPER scope")
            try:
                binding = self.order_index.resolve_intent(intent_id)
            except Exception as exc:
                raise ManagedRuntimeError("intent has no exact durable broker-order binding") from exc
            if (
                binding.account_ref != self.account_ref
                or binding.environment is not Environment.PAPER
                or binding.intent_id != intent_id
                or binding.requested_qty != intent.integer_qty
                or binding.side != intent.side
                or binding.symbol != intent.instrument_id
            ):
                raise ManagedRuntimeError("broker-order binding differs from the authorized PAPER intent")
            self._assert_sdk_healthy()
            cancel_id = "manager-cancel:" + hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:32]
            try:
                broker_order_id = self.ledger.start_cancel(
                    intent_id, cancel_id=cancel_id, now=datetime.now(timezone.utc)
                )
            except InvalidTransition as exc:
                raise ManagedRuntimeError("cancel is unavailable or already attempted; reconcile before retry") from exc
            try:
                self.process.cancel_order(broker_order_id)
            except Exception as exc:
                try:
                    self.ledger.mark_cancel_unknown(intent_id)
                except Exception as mark_exc:
                    raise ManagedRuntimeError(
                        "cancel outcome is unknown and could not be durably frozen"
                    ) from mark_exc
                raise ManagedRuntimeError("cancel outcome is unknown; reconcile before retry") from exc
            return self.ledger.get_order(intent_id)

    def stop(self) -> bool:
        """Stop only after the injected quiescence check and child drain succeed."""
        with self._lock:
            state, _reason = self.service.state()
            if state in {ExecutionControlState.RUNNING, ExecutionControlState.PAUSED}:
                self.service.pause(reason="managed_runtime_stop")
            return self.service.stop()

    def _wait_for_sdk_handshake(self) -> None:
        deadline = time.monotonic() + self.recovery_timeout
        while time.monotonic() < deadline:
            self._raise_worker_failure()
            if self.callback_pump.next_valid_id_seen.is_set() and self.callback_pump.managed_accounts_seen.is_set():
                self._assert_sdk_healthy()
                self.allocator.candidate_for_reconciliation()
                return
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("IBKR nextValidId and configured managedAccounts were not both verified")

    def _wait_for_snapshot(self, snapshot_id: int) -> None:
        deadline = time.monotonic() + self.snapshot_timeout
        while time.monotonic() < deadline:
            self._raise_worker_failure()
            self._assert_sdk_healthy()
            if self.snapshot_store.capture_complete(snapshot_id):
                self.coordinator.completed_capture(snapshot_id)  # validates required facts and schema
                return
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise TimeoutError("IBKR account, position, and open-order snapshot did not complete")

    def _assert_sdk_healthy(self) -> None:
        try:
            status = self.process.request("status")
        except Exception as exc:
            raise ManagedRuntimeError("IBKR SDK runtime status is unavailable") from exc
        if (
            status.get("state") != "RUNNING"
            or status.get("socket_connected") is not True
            or status.get("sdk_api_ready") is not True
            or status.get("callback_overflow") is not False
            or status.get("run_error") is not None
        ):
            raise ManagedRuntimeError("IBKR SDK runtime is not healthy and API-ready")

    def _raise_worker_failure(self) -> None:
        failure = self.callback_worker.failure
        if failure is not None:
            raise ManagedRuntimeError("durable callback consumer failed during runtime recovery") from failure

    def _recovery_proof_valid(self) -> bool:
        snapshot_id = self._latest_snapshot_id
        if (
            snapshot_id is None
            or self.callback_worker.failure is not None
            or not self.allocator.allocation_ready
        ):
            return False
        try:
            snapshot = self.snapshot_store.complete_snapshot(snapshot_id)
        except ReconciliationSnapshotError:
            return False
        if snapshot.get("reconciled") is not True:
            return False
        try:
            return self._external_recovery_check() is True
        except Exception:
            return False

    def _activation_ready(self) -> bool:
        try:
            self._assert_sdk_healthy()
            if not self.callback_pump.events.empty() or self.order_index.pending_callbacks():
                return False
            # Callback facts already committed and ACKed after reconciliation
            # are valid newer evidence. Require a still-fresh reconciliation
            # epoch and no raw callback work, but do not require the ledger to
            # equal the earlier snapshot byte-for-byte after such progress.
            self.latest_reconciled_snapshot()
        except ManagedRuntimeError:
            return False
        except Exception:
            return False
        return (
            self._recovery_proof_valid()
            and self._external_activation_check() is True
        )

    def _callback_worker_failed(self, failure: BaseException) -> None:
        state, _reason = self.service.state()
        if state in {ExecutionControlState.RUNNING, ExecutionControlState.PAUSED} and self.service.lease_held:
            try:
                self.service.pause(reason="ibkr_callback_consumer_failed")
            except Exception:
                pass
        if self._external_failure_sink is not None:
            self._external_failure_sink(failure)

    def _health_monitor_pause(self, reason: str) -> None:
        state, _previous_reason = self.service.state()
        if state in {ExecutionControlState.RUNNING, ExecutionControlState.PAUSED} and self.service.lease_held:
            self.service.pause(reason=reason)
        self._external_health_pause(reason)

    def _quiesce_and_shutdown(self) -> bool:
        try:
            if self._external_quiesce_check() is not True:
                return False
            if self._monitor_started and not self.health_monitor.stop(timeout=self.stop_timeout):
                return False
            self._monitor_started = False
            if not self._process_closed:
                self.process.close(timeout=self.stop_timeout)
                self._process_closed = True
            if self._worker_started:
                deadline = time.monotonic() + self.stop_timeout
                while time.monotonic() < deadline:
                    self._raise_worker_failure()
                    if not self.order_index.pending_callbacks():
                        break
                    time.sleep(self.poll_interval)
                if self.order_index.pending_callbacks() or self.order_index.pending_inbox():
                    return False
                if not self.callback_worker.stop(timeout=self.stop_timeout):
                    return False
                self._worker_started = False
            return True
        except Exception:
            # PaperExecutionService retains STOPPING and its account lease
            # whenever this callback fails or returns false.
            return False
