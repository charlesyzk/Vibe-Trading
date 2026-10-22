"""Managed IBKR/PAPER composition for a frozen Python strategy version.

This is deliberately separate from ``PythonPaperSession``: it uses only the
managed runtime's PAPER transports and reconciled account snapshot, and it
does not accept caller supplied order transports or synthetic starting cash.
Recovery restores exactly bound, reconciled PAPER BUY orders in the
acknowledged or partially-filled state. It can also stage one exact committed
outbox-linked CREATED BUY while PAUSED; explicit activation recreates a
temporary native ref and rechecks current risk. Ambiguous sends, callback
deliveries, fees, and all other CREATED/order states continue to fail closed.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import backtrader as bt

from src.bt_runtime.brokers.proxy import CallbackStatus, PortBroker, ReconciledFill, ReconciledOrderState
from src.bt_runtime.calendars import EXPECTED_CALENDAR_VERSION
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.backtrader_dispatch import BacktraderDispatchPort, OneShotLedgerIntentResolver
from src.execution.contracts import Bar, Environment, PositionMode, SourceKind
from src.execution.dispatch_service import PaperOrderDispatchService
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import PythonEventCoordinator
from src.execution.execution_service import ExecutionServiceNotReady
from src.execution.ibkr_callback_pump import BrokerCallback
from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime, ManagedRuntimeError
from src.execution.order_ledger import ExecutionControlState, LocalOrderState, SQLiteOrderLedger
from src.execution.outbox_dispatch import PythonOutboxIntentHandoff
from src.execution.paper_event_bridge import PythonPaperEventBridge
from src.execution.python_paper_session import _SessionStrategy
from src.execution.strategy_source_policy import StrategySourcePolicyError, validate_strategy_source
from src.execution.trusted_template_paper import TrustedTemplateEventCoordinator
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


class ManagedPythonPaperSessionError(RuntimeError):
    """A managed Python PAPER session cannot be safely composed or operated."""


class CallbackSinkRouter:
    """Bounded callback staging point before the Backtrader broker is ready."""

    def __init__(self, *, capacity: int = 256) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("callback router capacity must be a positive integer")
        self.capacity = capacity
        self._lock = threading.RLock()
        self._sink: Callable[[BrokerCallback], None] | None = None
        self._pending: dict[str, BrokerCallback] = {}
        self._recovery_barrier = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def __call__(self, callback: BrokerCallback) -> None:
        if not isinstance(callback, BrokerCallback):
            raise TypeError("callback router accepts normalized BrokerCallback values only")
        with self._lock:
            if self._recovery_barrier:
                raise ManagedPythonPaperSessionError(
                    "broker callback arrived before the recovered native order registry was ready"
                )
            sink = self._sink
            if sink is None:
                previous = self._pending.get(callback.event_id)
                if previous is not None:
                    if previous != callback:
                        raise ManagedPythonPaperSessionError(
                            "callback router event_id was reused with different content"
                        )
                    return
                if len(self._pending) >= self.capacity:
                    raise ManagedPythonPaperSessionError("callback router staging queue is full")
                self._pending[callback.event_id] = callback
                return
            # Keep queue acceptance ordered with attach()/flush and between
            # callbacks arriving concurrently from a managed worker.
            sink(callback)

    def enable_recovery_barrier(self) -> None:
        """Fail closed on callback delivery until exact native refs are restored."""
        with self._lock:
            if self._sink is not None or self._pending:
                raise ManagedPythonPaperSessionError("recovery barrier must be enabled before callback routing")
            self._recovery_barrier = True

    def release_recovery_barrier(self) -> None:
        """Allow deliveries only after the broker registry has been rebuilt."""
        with self._lock:
            if not self._recovery_barrier or self._sink is None or self._pending:
                raise ManagedPythonPaperSessionError("callback recovery barrier is not ready to release")
            self._recovery_barrier = False

    def attach(self, sink: Callable[[BrokerCallback], None]) -> None:
        """Attach the native Broker queue and flush staged values in order."""
        if not callable(sink):
            raise TypeError("callback sink must be callable")
        with self._lock:
            if self._sink is not None and self._sink is not sink:
                raise ManagedPythonPaperSessionError("callback router already has a different sink")
            pending = list(self._pending.items())
            for event_id, callback in pending:
                sink(callback)
                self._pending.pop(event_id, None)
            self._sink = sink


class ManagedPythonPaperSession:
    """Compose a single frozen Python version with a recovered PAPER runtime.

    The caller supplies verified live feeds/conId mappings, the trusted
    per-order risk service, event worker, authorizer, and durable run journal.
    ``start_paused`` explicitly starts/reconciles the runtime and then starts
    Cerebro while dispatch remains PAUSED. The application manager remains the
    only intended authority for activation.
    """

    def __init__(
        self,
        *,
        runtime: IbkrManagedPaperRuntime,
        run_id: str,
        version_store: StrategyVersionStore,
        version_id: str,
        journal: SQLiteEventJournal,
        coordinator: PythonEventCoordinator,
        handoff: PythonOutboxIntentHandoff,
        dispatch: PaperOrderDispatchService,
        feeds: Mapping[str, PaperCompletedBarFeed],
        data_by_con_id: Mapping[int, Any],
        resolve_contract_identity: Callable[[str], Any],
        queue_size: int = 256,
        idle_poll_seconds: float = 0.05,
        now: Callable | None = None,
    ) -> None:
        self.record = self._validate(
            runtime=runtime, run_id=run_id, version_store=version_store,
            version_id=version_id, journal=journal, coordinator=coordinator,
            handoff=handoff, dispatch=dispatch, feeds=feeds,
            data_by_con_id=data_by_con_id,
            resolve_contract_identity=resolve_contract_identity,
            queue_size=queue_size, idle_poll_seconds=idle_poll_seconds,
        )
        self.runtime = runtime
        self.run_id = run_id
        self.version_id = version_id
        self.version_store = version_store
        self.journal = journal
        self.coordinator = coordinator
        self.handoff = handoff
        self.dispatch_service = dispatch
        self.feeds = dict(feeds)
        self.data_by_con_id = dict(data_by_con_id)
        self.resolve_contract_identity = resolve_contract_identity
        self.queue_size = queue_size
        self.idle_poll_seconds = idle_poll_seconds
        self.now = now or next(iter(feeds.values()))._now
        if isinstance(coordinator, PythonEventCoordinator):
            coordinator.bind_trusted_clock(self.now)
        self.callback_router = CallbackSinkRouter(capacity=queue_size)
        self.callback_router.enable_recovery_barrier()
        if runtime._started or runtime.callback_worker.alive:
            raise ManagedPythonPaperSessionError(
                "managed session must bind callback routing before runtime startup"
            )
        # The worker may receive broker facts during snapshot/recovery. Keep
        # them durable and staged until the native broker is constructed.
        runtime.callback_pump.defer_delivery_ack = True
        runtime.callback_pump.callback_sink = self.callback_router
        self._runtime_callback_failure_sink = runtime.callback_worker.failure_sink
        runtime.callback_worker.failure_sink = self._callback_worker_failed
        self.strategy: _SessionStrategy | None = None
        self.port: BacktraderDispatchPort | None = None
        self.broker: PortBroker | None = None
        self.cerebro: bt.Cerebro | None = None
        self._started = False
        self._stopping = False
        self._finished = False
        self._exception: BaseException | None = None
        self._recovery_ready = False
        self._recovery_event: tuple[Any, Any] | None = None
        self._recovery_created_intent_id: str | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _callback_worker_failed(self, failure: BaseException) -> None:
        """Pause the session journal as well as the runtime after a fatal callback.

        IBKR callback consumption is deliberately independent of the strategy
        thread. When that trusted consumer fails, the runtime closes its
        account dispatch gate, while this session also prevents its durable
        strategy run from releasing more events. Keep the underlying failure
        local for diagnostics; status surfaces expose only the stable health
        code.
        """
        with self._lock:
            if self._exception is None or self._recovery_ready:
                self._exception = failure
                self._recovery_ready = False
        try:
            self.journal.pause_run(self.run_id)
        except Exception:
            # The in-memory health code remains visible if durable storage is
            # unavailable; do not mask the runtime's account-scope pause.
            pass
        if self._runtime_callback_failure_sink is not None:
            self._runtime_callback_failure_sink(failure)

    @staticmethod
    def _validate(
        *, runtime, run_id, version_store, version_id, journal, coordinator,
        handoff, dispatch, feeds, data_by_con_id, resolve_contract_identity,
        queue_size, idle_poll_seconds,
    ) -> StrategyVersionRecord:
        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("runtime must be an IbkrManagedPaperRuntime")
        if runtime._started or runtime.callback_worker.alive:
            raise ManagedPythonPaperSessionError("runtime must be unstarted when session is composed")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        if not isinstance(version_store, StrategyVersionStore):
            raise TypeError("version_store must be a StrategyVersionStore")
        record = version_store.get_version(version_id)
        if record.source_kind is SourceKind.PYTHON_UPLOAD:
            try:
                source_text = version_store.read_upload_source(version_id).decode("utf-8", errors="strict")
                validate_strategy_source(source_text)
            except (UnicodeDecodeError, StrategySourcePolicyError) as exc:
                raise ManagedPythonPaperSessionError(
                    "frozen strategy source is outside the supported upload policy"
                ) from exc
            if record.spec.position_policy.mode is not PositionMode.EXPLICIT_QUANTITY:
                raise ManagedPythonPaperSessionError(
                    "managed Python upload session requires explicit whole-share quantities"
                )
            if not isinstance(coordinator, PythonEventCoordinator):
                raise TypeError("Python upload versions require PythonEventCoordinator")
        elif record.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE:
            from src.bt_runtime.strategies.sma import (
                TRUSTED_TEMPLATE_COMPILER_VERSION,
                TRUSTED_TEMPLATE_SOURCE,
                TRUSTED_TEMPLATE_SOURCE_SHA256,
                compile_strategy,
            )
            from src.execution.versions import VersionStoreError

            try:
                source = version_store.read_template_source(version_id)
                if (
                    record.source_hash != TRUSTED_TEMPLATE_SOURCE_SHA256
                    or record.compiler_version != TRUSTED_TEMPLATE_COMPILER_VERSION
                    or record.engine_version != f"backtrader/{bt.__version__}"
                ):
                    raise ValueError("template provenance")
                compile_strategy(record.spec)
            except (UnicodeDecodeError, ValueError, TypeError, VersionStoreError) as exc:
                raise ManagedPythonPaperSessionError(
                    "frozen trusted template is outside the installed compiler/version policy"
                ) from exc
            if source != TRUSTED_TEMPLATE_SOURCE:
                raise ManagedPythonPaperSessionError("frozen trusted template artifact differs from installed code")
            if record.spec.position_policy.mode is not PositionMode.TARGET_FRACTION:
                raise ManagedPythonPaperSessionError("managed trusted template requires target_fraction sizing")
            if not isinstance(coordinator, TrustedTemplateEventCoordinator):
                raise TypeError("trusted template versions require TrustedTemplateEventCoordinator")
        else:
            raise ManagedPythonPaperSessionError("managed PAPER session rejects unsupported strategy source kinds")
        if len(record.spec.instrument_ids) != 1 or record.spec.execution_policy.outside_rth:
            raise ManagedPythonPaperSessionError("managed PAPER V1 requires one RTH instrument")
        if not isinstance(journal, SQLiteEventJournal) or not isinstance(
            coordinator, (PythonEventCoordinator, TrustedTemplateEventCoordinator)
        ):
            raise TypeError("journal and coordinator must be trusted execution collaborators")
        if not isinstance(handoff, PythonOutboxIntentHandoff) or not isinstance(dispatch, PaperOrderDispatchService):
            raise TypeError("handoff and dispatch must be trusted execution collaborators")
        if (
            coordinator.versions is not version_store
            or coordinator.journal is not journal
            or handoff.journal is not journal
            or handoff.ledger is not runtime.ledger
            or dispatch.ledger is not runtime.ledger
        ):
            raise ManagedPythonPaperSessionError("session collaborators do not share the runtime stores")
        if runtime.callback_pump.ledger is not runtime.ledger:
            raise ManagedPythonPaperSessionError("callback pump must use the managed runtime ledger")
        if (
            runtime.callback_pump.order_index is not runtime.order_index
            or runtime.callback_worker.pump is not runtime.callback_pump
        ):
            raise ManagedPythonPaperSessionError("runtime callback worker/pump/index identity differs")
        if not callable(getattr(runtime, "paper_transport", None)) or not callable(
            getattr(runtime, "cancel_transport", None)
        ):
            raise ManagedPythonPaperSessionError("managed runtime must provide its own PAPER submit and cancel transports")
        canonical_db = Path(runtime.ledger.path).expanduser().resolve()
        if (
            Path(runtime.order_index.path).expanduser().resolve() != canonical_db
            or Path(journal.path).expanduser().resolve() != canonical_db
            or Path(dispatch.ledger.path).expanduser().resolve() != canonical_db
        ):
            raise ManagedPythonPaperSessionError("strategy journal and runtime execution ledger must share SQLite")
        identity = journal.get_run_identity(run_id, allow_paused=True)
        if (
            identity["strategy_version_hash"] != record.version_hash
            or identity["account_ref"] != runtime.account_ref
            or identity["environment"] != Environment.PAPER.value
        ):
            raise ManagedPythonPaperSessionError("run identity differs from the frozen PAPER runtime/version")
        if not isinstance(feeds, Mapping) or set(feeds) != set(record.spec.instrument_ids):
            raise ManagedPythonPaperSessionError("verified feed mapping must exactly match the frozen instrument set")
        policy = record.spec.bar_policy
        if (
            policy.timeframe != "1d"
            or policy.session != "regular"
            or policy.completed_only is not True
            or policy.calendar_id != "us_equity_rth"
            or policy.timezone != "America/New_York"
        ):
            raise ManagedPythonPaperSessionError("frozen strategy bar policy is outside the supported XNYS daily session")
        if not isinstance(data_by_con_id, Mapping) or not data_by_con_id:
            raise ManagedPythonPaperSessionError("an explicit verified conId-to-feed map is required")
        feeds_by_identity = {id(feed) for feed in feeds.values()}
        for instrument, feed in feeds.items():
            if (
                not isinstance(feed, PaperCompletedBarFeed)
                or feed.p.instrument_id != instrument
                or feed.p.timezone != policy.timezone
                or feed.p.calendar_id != policy.calendar_id
                or feed.p.calendar_version != EXPECTED_CALENDAR_VERSION
            ):
                raise ManagedPythonPaperSessionError("each frozen instrument requires its matching trusted completed-bar feed")
        seen_feed_ids: set[int] = set()
        for contract_id, data in data_by_con_id.items():
            if type(contract_id) is not int or contract_id <= 0 or data is None:
                raise ManagedPythonPaperSessionError("conId mapping requires positive ids and Backtrader feeds")
            if id(data) not in feeds_by_identity:
                raise ManagedPythonPaperSessionError("conId mapping references a feed outside this frozen session")
            if id(data) in seen_feed_ids:
                raise ManagedPythonPaperSessionError("one Backtrader feed cannot map to multiple conIds")
            seen_feed_ids.add(id(data))
        if not callable(resolve_contract_identity):
            raise TypeError("a verified contract identity resolver is required")
        for instrument in record.spec.instrument_ids:
            contract = resolve_contract_identity(instrument)
            if (
                type(getattr(contract, "contract_id", None)) is not int
                or contract.contract_id not in data_by_con_id
                or data_by_con_id[contract.contract_id] is not feeds[instrument]
                or getattr(contract, "symbol", None) != instrument
                or getattr(contract, "security_type", None) != "STK"
                or getattr(contract, "currency", None) != "USD"
            ):
                raise ManagedPythonPaperSessionError("strategy instrument lacks its verified conId/feed mapping")
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not 0.01 <= idle_poll_seconds <= 5
        ):
            raise ValueError("idle_poll_seconds must be between 10 ms and 5 seconds")
        return record

    def start_paused(self) -> dict[str, Any]:
        """Connect/reconcile, hydrate from a fresh snapshot, then idle Cerebro PAUSED."""
        with self._lock:
            try:
                return self._start_paused_impl()
            except Exception:
                # Once runtime startup begins, every failure must leave the
                # durable run and execution gate closed. Keep the owner lease
                # held so a later manager action can reconcile safely.
                self._pause_run_best_effort()
                try:
                    state, _reason = self.runtime.service.state()
                    if state is ExecutionControlState.RUNNING and self.runtime.service.lease_held:
                        self.runtime.pause(reason="managed_session_start_failed")
                except Exception:
                    pass
                raise

    def _start_paused_impl(self) -> dict[str, Any]:
        """Perform startup while the public method owns the session lock."""
        with self._lock:
            if self._started:
                raise ManagedPythonPaperSessionError("managed PAPER session can only start once")
            self.runtime.start_paused()
            if not self.runtime.callback_worker.pause(timeout=self.runtime.stop_timeout):
                raise ManagedPythonPaperSessionError(
                    "callback worker could not quiesce for native order recovery"
                )
            state, _ = self.runtime.service.state()
            if state is not ExecutionControlState.PAUSED or not self.runtime.service.lease_held:
                raise ManagedPythonPaperSessionError("managed runtime must finish recovery PAUSED with its lease")
            try:
                snapshot = self.runtime.latest_reconciled_snapshot()
            except ManagedRuntimeError as exc:
                raise ManagedPythonPaperSessionError("runtime has no fresh reconciled account snapshot") from exc
            try:
                self.runtime.reconciler.verify_reconciled_snapshot_is_current(snapshot)
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "ledger changed during snapshot recovery; native orders remain unattached"
                ) from exc
            if isinstance(self.coordinator, PythonEventCoordinator):
                try:
                    self.coordinator.shadow_replay_committed_prefix(self.run_id)
                except Exception as exc:
                    self.journal.pause_run(self.run_id)
                    raise ManagedPythonPaperSessionError(
                        "committed Python event prefix failed shadow replay"
                    ) from exc

            try:
                self._recover_committed_outbox_handoffs()
                recovered_created = self._committed_created_recovery_candidates()
                self._reject_unrestorable_recovery_state(
                    allowed_created_intent_ids={item["intent_id"] for item in recovered_created},
                )
            except Exception:
                self._pause_run_best_effort()
                raise

            resolver = OneShotLedgerIntentResolver(self.runtime.ledger)
            self.port = BacktraderDispatchPort(
                ledger=self.runtime.ledger,
                dispatch=self.dispatch_service,
                callback_pump=self.runtime.callback_pump,
                resolve_intent=resolver,
                resolve_contract_identity=self.resolve_contract_identity,
                owner_lease=self.runtime.service.require_owner_lease(),
                allocate_order_id=self.runtime.allocator.allocate,
                observe_next_valid_id=self.runtime.allocator.observe_next_valid_id,
                transport=self.runtime.paper_transport,
                cancel_transport=self.runtime.cancel_transport,
                now=self.now,
                callback_worker_owns_pump=True,
            )
            self.port.register_callback_sink(self.callback_router)
            self.broker = PortBroker(
                self.port,
                callback_queue_size=self.queue_size,
                initial_cash=0.0,
                recovered_dispatch_ready=lambda: (
                    self.runtime.service.state()[0] is ExecutionControlState.RUNNING
                    and self.runtime.service.lease_held
                ),
            )
            self.callback_router.attach(self.broker.on_callback)
            self._reject_pending_callback_race()
            self.cerebro = bt.Cerebro(
                stdstats=False, preload=False, runonce=False, quicknotify=True,
            )
            for instrument, feed in self.feeds.items():
                self.cerebro.adddata(feed, name=instrument)
            self.cerebro.setbroker(self.broker)
            self.broker.hydrate_reconciled_account_snapshot(
                snapshot,
                account_ref=self.runtime.account_ref,
                environment=Environment.PAPER.value,
                data_by_con_id=self.data_by_con_id,
            )
            for state in self._reconciled_order_states(snapshot):
                self.broker.restore_reconciled_order(state, data=self.feeds[state.instrument])
            self._queue_recovered_created_intents(recovered_created)
            self.callback_router.release_recovery_barrier()
            if (
                self.runtime.order_index.pending_inbox()
                or self.runtime.order_index.pending_callbacks()
                or not self.broker._callback_queue.empty()
            ):
                raise ManagedPythonPaperSessionError(
                    "callback delivery changed during native order recovery"
                )
            bridge = PythonPaperEventBridge(
                run_id=self.run_id,
                journal=self.journal,
                ledger=self.runtime.ledger,
                coordinator=self.coordinator,
                handoff=self.handoff,
                execution_service=self.runtime.service,
                intent_resolver=resolver,
            )
            self.cerebro.addstrategy(_SessionStrategy, bridge=bridge)
            self._started = True
            self._thread = threading.Thread(
                target=self._run_cerebro,
                name=f"managed-python-paper-{self.run_id}",
                daemon=False,
            )
            self._thread.start()
            self.runtime.callback_worker.resume()
            return snapshot

    def recover_failed_session(self) -> dict[str, Any]:
        """Rebuild Cerebro after a strategy-thread failure, leaving dispatch PAUSED.

        This recovery is deliberately manager-owned and must be called only
        after an attributable ``recover`` grant. It accepts the supported
        BUY recovery slice, replays unACKed callbacks into restored native
        refs, and saves the exact released event for a later explicit activate.
        """
        with self._lock:
            thread = self._thread
            if self._exception is None or thread is None or thread.is_alive():
                raise ManagedPythonPaperSessionError(
                    "recovery requires a failed strategy thread that has fully exited"
                )
            if self._stopping or self._finished or self._recovery_ready:
                raise ManagedPythonPaperSessionError("session is already stopping, finished, or recovered")
            state, _reason = self.runtime.service.state()
            if state is not ExecutionControlState.PAUSED or not self.runtime.service.lease_held:
                raise ManagedPythonPaperSessionError("recovery requires a PAUSED runtime with its owner lease")
            worker = self.runtime.callback_worker
            if not worker.alive or worker.failure is not None:
                raise ManagedPythonPaperSessionError("recovery requires a healthy live callback worker")
            old_broker = self.broker
            if old_broker is None or old_broker._callback_error is not None:
                raise ManagedPythonPaperSessionError("failed Backtrader broker is not safe to replace")
            if self.callback_router.pending_count:
                raise ManagedPythonPaperSessionError("old callback router has untransferred staged callbacks")

            try:
                retry_event = self.journal.inspect_paused_released_event(self.run_id)
                if set(retry_event.payload) != {"bar"}:
                    raise ValueError("event payload shape")
                retry_bar = Bar.model_validate(retry_event.payload["bar"])
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "recovery requires one integrity-checked paused released bar"
                ) from exc
            old_feed = self.feeds.get(retry_bar.instrument_id)
            if (
                old_feed is None
                or old_feed.current_completed_bar != retry_bar
                or any(feed.pending_bar_count for feed in self.feeds.values())
            ):
                raise ManagedPythonPaperSessionError(
                    "failed feed cursor differs from the exact durable recovery bar"
                )

            if not worker.pause(timeout=self.runtime.stop_timeout):
                raise ManagedPythonPaperSessionError("callback worker could not quiesce before recovery")
            try:
                pending_ids = self.runtime.order_index.pending_delivery_event_ids()
                # The old Cerebro thread has exited. Keep broker events durable
                # while fresh reconciliation consumes snapshot callbacks.
                worker.rebind_delivery_sink(
                    lambda _callback: None,
                    expected_pending_event_ids=pending_ids,
                )
                worker.resume()
                self.runtime.start_paused()
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "fresh broker reconciliation failed during session recovery"
                ) from exc

            if not worker.pause(timeout=self.runtime.stop_timeout):
                raise ManagedPythonPaperSessionError("callback worker could not quiesce after reconciliation")
            state, _reason = self.runtime.service.state()
            if state is not ExecutionControlState.PAUSED or not self.runtime.service.lease_held:
                raise ManagedPythonPaperSessionError("recovery did not retain PAUSED state and owner lease")
            if worker.failure is not None:
                raise ManagedPythonPaperSessionError("callback worker failed during session recovery")
            if not self.runtime.callback_pump.events.empty() or self.runtime.order_index.pending_callbacks():
                raise ManagedPythonPaperSessionError(
                    "raw callback queue and spool must be drained before session recovery"
                )
            try:
                snapshot = self.runtime.latest_reconciled_snapshot()
                self.runtime.reconciler.verify_reconciled_snapshot_is_current(snapshot)
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "account snapshot changed before callback quiescence"
                ) from exc
            if isinstance(self.coordinator, PythonEventCoordinator):
                try:
                    self.coordinator.shadow_replay_committed_prefix(self.run_id)
                except Exception as exc:
                    self.journal.pause_run(self.run_id)
                    raise ManagedPythonPaperSessionError(
                        "committed Python event prefix failed shadow replay during recovery"
                    ) from exc

            try:
                self._recover_committed_outbox_handoffs()
                recovered_created = self._committed_created_recovery_candidates()
                self._reject_unrestorable_recovery_state(
                    allow_pending_inbox=True,
                    allowed_created_intent_ids={item["intent_id"] for item in recovered_created},
                )
            except Exception:
                self._pause_run_best_effort()
                raise
            recovery = self.runtime.ledger.recovery_snapshot_for_scope(
                broker="IBKR", account_ref=self.runtime.account_ref, environment=Environment.PAPER,
            )
            if recovered_created and recovery.active_orders:
                raise ManagedPythonPaperSessionError(
                    "active broker orders and CREATED intents cannot be recovered together"
                )
            active_intents = {record.intent_id for record in recovery.active_orders}
            pending_inbox = self.runtime.order_index.pending_inbox()
            if any(event.intent_id not in active_intents for _source_key, event in pending_inbox):
                raise ManagedPythonPaperSessionError(
                    "unACKed callback is outside the supported active-order recovery slice"
                )
            pending_ids = self.runtime.order_index.pending_delivery_event_ids()
            with old_broker._callback_queue.mutex:
                old_queued_callbacks = tuple(old_broker._callback_queue.queue)
            if any(callback.event_id not in pending_ids for callback in old_queued_callbacks):
                raise ManagedPythonPaperSessionError(
                    "old broker queue contains a callback without a durable replay source"
                )
            if old_broker._notifications:
                raise ManagedPythonPaperSessionError(
                    "old broker has strategy notifications that cannot be reconstructed"
                )
            fees = self.runtime.ledger.list_fee_facts(
                broker="IBKR", account_ref=self.runtime.account_ref,
                environment=Environment.PAPER,
            )
            if any(
                fact["association_status"] == "PENDING_EXECUTION"
                or fact["intent_id"] in active_intents
                for fact in fees
            ):
                raise ManagedPythonPaperSessionError(
                    "pending or active-order commission facts block session recovery"
                )

            old_feeds = self.feeds
            try:
                fresh_feeds = {
                    instrument: type(feed)(**dict(feed.p._getkwargs()))
                    for instrument, feed in old_feeds.items()
                }
                fresh_data_by_con_id = {}
                for contract_id, old_data in self.data_by_con_id.items():
                    instrument = next(
                        (name for name, feed in old_feeds.items() if feed is old_data), None,
                    )
                    if instrument is None:
                        raise ValueError("contract mapping has no source feed")
                    fresh_data_by_con_id[contract_id] = fresh_feeds[instrument]
            except Exception as exc:
                raise ManagedPythonPaperSessionError("replacement feed set could not be rebuilt") from exc

            resolver = OneShotLedgerIntentResolver(self.runtime.ledger)
            router = CallbackSinkRouter(capacity=self.queue_size)
            router.enable_recovery_barrier()
            self.feeds = fresh_feeds
            self.data_by_con_id = fresh_data_by_con_id
            self.callback_router = router
            self.port = BacktraderDispatchPort(
                ledger=self.runtime.ledger,
                dispatch=self.dispatch_service,
                callback_pump=self.runtime.callback_pump,
                resolve_intent=resolver,
                resolve_contract_identity=self.resolve_contract_identity,
                owner_lease=self.runtime.service.require_owner_lease(),
                allocate_order_id=self.runtime.allocator.allocate,
                observe_next_valid_id=self.runtime.allocator.observe_next_valid_id,
                transport=self.runtime.paper_transport,
                cancel_transport=self.runtime.cancel_transport,
                now=self.now,
                callback_worker_owns_pump=True,
            )
            self.broker = PortBroker(
                self.port,
                callback_queue_size=self.queue_size,
                initial_cash=0.0,
                recovered_dispatch_ready=lambda: (
                    self.runtime.service.state()[0] is ExecutionControlState.RUNNING
                    and self.runtime.service.lease_held
                ),
                on_broker_cycle=self._resume_recovery_bar_after_created_intent,
            )
            router.attach(self.broker.on_callback)
            # Register the rebuilt feeds before restoring orders. Cerebro assigns
            # each feed's frozen instrument name during adddata; the broker
            # validates that identity when attaching a recovered native ref.
            cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)
            for instrument, feed in self.feeds.items():
                cerebro.adddata(feed, name=instrument)
            cerebro.setbroker(self.broker)
            self.broker.hydrate_reconciled_account_snapshot(
                snapshot,
                account_ref=self.runtime.account_ref,
                environment=Environment.PAPER.value,
                data_by_con_id=self.data_by_con_id,
            )
            for order_state in self._reconciled_order_states(snapshot, allow_paused_run=True):
                self.broker.restore_reconciled_order(
                    order_state, data=self.feeds[order_state.instrument],
                )
            self._queue_recovered_created_intents(recovered_created)
            router.release_recovery_barrier()
            if self.runtime.order_index.pending_delivery_event_ids() != pending_ids:
                raise ManagedPythonPaperSessionError("durable callback identities changed during native recovery")
            worker.rebind_delivery_sink(
                router,
                expected_pending_event_ids=pending_ids,
            )
            self.port.register_callback_sink(router)

            bridge = PythonPaperEventBridge(
                run_id=self.run_id,
                journal=self.journal,
                ledger=self.runtime.ledger,
                coordinator=self.coordinator,
                handoff=self.handoff,
                execution_service=self.runtime.service,
                intent_resolver=resolver,
            )
            cerebro.addstrategy(_SessionStrategy, bridge=bridge)
            self.cerebro = cerebro
            self._recovery_event = (retry_event, retry_bar)
            self._recovery_created_intent_id = (
                recovered_created[0]["intent_id"] if recovered_created else None
            )
            self._thread = threading.Thread(
                target=self._run_cerebro,
                name=f"managed-python-paper-recovery-{self.run_id}",
                daemon=False,
            )
            self._thread.start()
            worker.resume()
            self._recovery_ready = True
            return snapshot

    @property
    def service(self):
        """Expose the one durable service scope to PaperExecutionManager."""
        return self.runtime.service

    @property
    def durable_database_paths(self) -> tuple[str, ...]:
        """Report the shared execution database as the manager's scope proof."""
        return self.runtime.durable_database_paths

    def activate(self) -> None:
        """Open the durable send gate through the managed runtime."""
        with self._lock:
            if self._exception is not None and not self._recovery_ready:
                raise ManagedPythonPaperSessionError(
                    "managed session failed; rebuild the session and reconcile before activation"
                )
            if not self._started or self._stopping or self._finished:
                raise ManagedPythonPaperSessionError("managed session is not ready for activation")
            if self._recovery_ready:
                if self._recovery_event is None:
                    raise ManagedPythonPaperSessionError("recovered session has no exact event to retry")
                event, bar = self._recovery_event
                # Open the service gate only after fresh account/native state
                # recovery. If an exact CREATED order also needs to be sent,
                # keep the released strategy bar staged until that order has
                # a terminal broker result; otherwise later strategy output
                # could race an unresolved send and duplicate the prior action.
                self.runtime.activate()
                try:
                    if self._recovery_created_intent_id is None:
                        self._resume_recovery_event(event, bar)
                    else:
                        self._resume_recovery_bar_after_created_intent()
                except Exception:
                    try:
                        self.journal.pause_run(self.run_id)
                    finally:
                        self.runtime.pause(reason="managed_session_recovery_activation_failed")
                    raise
                self._exception = None
                self._recovery_ready = False
            else:
                self.runtime.activate()
                self._resume_recovery_bar_after_created_intent()

    def _resume_recovery_event(self, event, bar) -> None:
        self.journal.resume_paused_released_event(
            self.run_id,
            expected_event_id=event.event_id,
            expected_seq=event.seq,
            expected_payload_sha256=event.payload_sha256,
        )
        self.feeds[bar.instrument_id].push_completed_bar(bar)
        self._recovery_event = None
        self._recovery_created_intent_id = None

    def _resume_recovery_bar_after_created_intent(self) -> None:
        """Release the exact failed bar only after the staged order is terminal.

        This callback runs from Cerebro's broker cycle, after durable broker
        callbacks have been applied on the same thread. A send that remains
        SEND_STARTED/UNKNOWN, or an active acknowledged/partial order, keeps
        the released event untouched and unprocessed.
        """
        with self._lock:
            if self._recovery_event is None or self._recovery_created_intent_id is None:
                return
            state, _reason = self.runtime.service.state()
            if state is not ExecutionControlState.RUNNING or not self.runtime.service.lease_held:
                return
            order = self.runtime.ledger.get_order(self._recovery_created_intent_id)
            if order.state in {LocalOrderState.CANCELLED, LocalOrderState.REJECTED}:
                raise ManagedPythonPaperSessionError(
                    "recovered CREATED order did not fill; fresh reconciliation is required"
                )
            if order.state not in {
                LocalOrderState.FILLED,
            }:
                return
            event, bar = self._recovery_event
            self._resume_recovery_event(event, bar)

    def _committed_created_recovery_candidates(self) -> list[dict[str, Any]]:
        """Return the one exact linked CREATED intent safe to stage while PAUSED."""
        recovery = self.runtime.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=self.runtime.account_ref, environment=Environment.PAPER,
        )
        records = recovery.ready_to_reserve
        if not records:
            return []
        if len(records) != 1:
            raise ManagedPythonPaperSessionError(
                "multiple CREATED intents block exact native-order recovery"
            )
        record = records[0]
        try:
            intent = self.runtime.ledger.get_intent(record.intent_id)
            identity = self.journal.get_run_identity(self.run_id, allow_paused=True)
            linked_identity = self.journal.require_linked_outbox_intent(
                self.run_id,
                intent.input_event_seq,
                intent.action_index,
                record.intent_id,
                intent=intent.model_dump(mode="json"),
                allow_paused=True,
            )
        except Exception as exc:
            raise ManagedPythonPaperSessionError(
                "CREATED intent lacks an exact committed strategy outbox link"
            ) from exc
        if linked_identity != identity:
            raise ManagedPythonPaperSessionError(
                "CREATED intent outbox identity differs from the frozen strategy run"
            )
        if (
            record.state is not LocalOrderState.CREATED
            or intent.broker != "IBKR"
            or intent.account_ref != self.runtime.account_ref
            or intent.environment is not Environment.PAPER
            or intent.deployment_id != identity["deployment_id"]
            or intent.strategy_version_hash != self.record.version_hash
            or intent.side != "BUY"
            or intent.order_type != "market"
            or intent.time_in_force != "DAY"
            or intent.instrument_id not in self.feeds
            or type(intent.integer_qty) is not int
            or intent.integer_qty <= 0
        ):
            raise ManagedPythonPaperSessionError(
                "CREATED intent differs from the supported frozen PAPER order scope"
            )
        return [{
            "intent_id": record.intent_id,
            "instrument": intent.instrument_id,
            "side": intent.side,
            "quantity": intent.integer_qty,
            "reference_price": float(intent.reference_price),
        }]

    def _queue_recovered_created_intents(self, candidates: list[dict[str, Any]]) -> None:
        if candidates and self.broker is None:
            raise ManagedPythonPaperSessionError("native broker must hydrate before CREATED intent staging")
        for candidate in candidates:
            try:
                self.broker.queue_recovered_intent(
                    data=self.feeds[candidate["instrument"]],
                    instrument=candidate["instrument"],
                    side=candidate["side"],
                    quantity=candidate["quantity"],
                    reference_price=candidate["reference_price"],
                    intent_id=candidate["intent_id"],
                )
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "exact CREATED intent could not be staged for explicit activation"
                ) from exc

    def _reject_unrestorable_recovery_state(
        self, *, allow_pending_inbox: bool = False,
        allowed_created_intent_ids: set[str] | None = None,
    ) -> None:
        recovery = self.runtime.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=self.runtime.account_ref, environment=Environment.PAPER,
        )
        allowed_created_intent_ids = allowed_created_intent_ids or set()
        unrestorable_created = [
            record for record in recovery.ready_to_reserve
            if record.intent_id not in allowed_created_intent_ids
        ]
        if (
            unrestorable_created
            or recovery.ready_to_send
            or recovery.unresolved_sends
        ):
            raise ManagedPythonPaperSessionError(
                "active or unresolved ledger work outside the exact recovery slice blocks managed session recovery"
            )
        unsupported = [record for record in recovery.active_orders if record.state not in {
            LocalOrderState.ACKNOWLEDGED, LocalOrderState.PARTIALLY_FILLED,
        }]
        if unsupported:
            raise ManagedPythonPaperSessionError(
                "active order state is outside the exact ACKNOWLEDGED/PARTIALLY_FILLED recovery slice"
            )
        if self.runtime.order_index.pending_callbacks():
            raise ManagedPythonPaperSessionError(
                "unprocessed raw callback spool blocks managed session recovery"
            )
        if self.runtime.order_index.pending_inbox() and not allow_pending_inbox:
            raise ManagedPythonPaperSessionError(
                "unacknowledged callback recovery blocks session attach until native orders can be restored"
            )
        if self.callback_router.pending_count:
            raise ManagedPythonPaperSessionError(
                "staged broker callbacks cannot be mapped to a new native Backtrader order registry"
            )

    def _recover_committed_outbox_handoffs(self) -> None:
        """Finish committed proposal-to-ledger writes after integrity replay.

        This only creates/links CREATED ledger intents. It never reserves cash
        or calls a sender. A later exact-link validator stages at most one such
        intent for dispatch only after explicit activation on Cerebro's thread.
        """
        try:
            processed = 0
            while True:
                pending = self.journal.list_dispatchable_outbox(self.run_id, include_paused=True)
                if not pending:
                    return
                if processed >= 1000:
                    raise ManagedPythonPaperSessionError("committed strategy outbox exceeds recovery bound")
                result = self.handoff.process_next(self.run_id, allow_paused=True)
                if result is None:
                    raise ManagedPythonPaperSessionError(
                        "committed strategy outbox changed during recovery"
                    )
                processed += 1
        except Exception:
            self._pause_run_best_effort()
            raise

    def _pause_run_best_effort(self) -> None:
        try:
            self.journal.pause_run(self.run_id)
        except Exception:
            pass

    def _reconciled_order_states(
        self, snapshot: Mapping[str, Any], *, allow_paused_run: bool = False,
    ) -> list[ReconciledOrderState]:
        """Build strict native-order facts from one reconciled ledger/snapshot pair."""
        recovery = self.runtime.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=self.runtime.account_ref, environment=Environment.PAPER,
        )
        active = list(recovery.active_orders)
        if not active:
            return []
        if self.broker is None:
            raise ManagedPythonPaperSessionError("native broker must hydrate before order recovery")

        position_by_contract = {row["contract_id"]: row for row in snapshot.get("positions", [])}
        active_intent_ids = {record.intent_id for record in active}
        fee_facts = self.runtime.ledger.list_fee_facts(
            broker="IBKR", account_ref=self.runtime.account_ref,
            environment=Environment.PAPER,
        )
        if any(
            fact["intent_id"] in active_intent_ids
            or fact["association_status"] == "PENDING_EXECUTION"
            for fact in fee_facts
        ):
            raise ManagedPythonPaperSessionError(
                "active-order commission facts are not represented in the native execution state"
            )

        states: list[ReconciledOrderState] = []
        run_identity = self.journal.get_run_identity(self.run_id, allow_paused=allow_paused_run)
        for record in active:
            try:
                intent = self.runtime.ledger.get_intent(record.intent_id)
                binding = self.runtime.order_index.resolve_intent(record.intent_id)
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "active order lacks an exact durable PAPER broker binding"
                ) from exc
            try:
                linked_run_identity = self.journal.require_linked_outbox_intent(
                    self.run_id,
                    intent.input_event_seq,
                    intent.action_index,
                    record.intent_id,
                    intent=intent.model_dump(mode="json"),
                    allow_paused=allow_paused_run,
                )
            except Exception as exc:
                raise ManagedPythonPaperSessionError(
                    "active order lacks an exact committed outbox link in the current run"
                ) from exc
            if linked_run_identity != run_identity:
                raise ManagedPythonPaperSessionError(
                    "active order outbox run identity differs from the current strategy run"
                )
            if (
                intent.broker != "IBKR"
                or intent.account_ref != self.runtime.account_ref
                or intent.environment is not Environment.PAPER
                or intent.side != "BUY"
                or intent.deployment_id != run_identity["deployment_id"]
                or intent.strategy_version_hash != self.record.version_hash
                or binding.account_ref != self.runtime.account_ref
                or binding.environment is not Environment.PAPER
                or binding.intent_id != record.intent_id
                or binding.requested_qty != intent.integer_qty
                or binding.side != "BUY"
                or binding.symbol != intent.instrument_id
                or binding.security_type != "STK"
                or binding.currency != "USD"
                or self.data_by_con_id.get(binding.contract_id) is not self.feeds.get(intent.instrument_id)
                or record.broker_order_id != str(binding.broker_order_id)
            ):
                raise ManagedPythonPaperSessionError(
                    "active order intent, broker binding, and verified feed differ"
                )

            fills = self.runtime.ledger.list_fills(record.intent_id)
            try:
                executions = tuple(sorted((
                    ReconciledFill(
                        execution_id=str(fill["broker_exec_id"]),
                        quantity=int(fill["quantity"]),
                        price=float(Decimal(str(fill["price"]))),
                        occurred_at=datetime.fromisoformat(str(fill["occurred_at"])),
                    ) for fill in fills
                ), key=lambda fill: (fill.occurred_at, fill.execution_id)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ManagedPythonPaperSessionError("durable per-execution facts are invalid") from exc
            fill_qty = sum(int(fill["quantity"]) for fill in fills)
            fill_notional = sum((
                Decimal(int(fill["quantity"])) * Decimal(str(fill["price"])) for fill in fills
            ), Decimal("0"))
            if fill_qty != record.filled_qty or (
                fill_qty and record.average_fill_price != fill_notional / fill_qty
            ):
                raise ManagedPythonPaperSessionError("durable fill rows differ from the active order aggregate")
            if record.state is LocalOrderState.ACKNOWLEDGED and fill_qty != 0:
                raise ManagedPythonPaperSessionError("acknowledged order unexpectedly has durable fills")
            if record.state is LocalOrderState.PARTIALLY_FILLED and not 0 < fill_qty < intent.integer_qty:
                raise ManagedPythonPaperSessionError("partial order has an invalid durable cumulative fill")

            position_row = position_by_contract.get(binding.contract_id)
            if position_row is None:
                position_size_after, position_average_price_after = 0, 0.0
            else:
                position_size = Decimal(str(position_row["quantity"]))
                position_average_price = Decimal(str(position_row["average_cost"]))
                if position_size != position_size.to_integral_value() or position_size < 0:
                    raise ManagedPythonPaperSessionError("reconciled position is not a whole-share long position")
                position_size_after = int(position_size)
                position_average_price_after = float(position_average_price)
            if fill_qty and position_size_after < fill_qty:
                raise ManagedPythonPaperSessionError("reconciled position cannot contain the durable BUY fills")

            occurred_at = executions[-1].occurred_at if executions else None

            states.append(ReconciledOrderState(
                order_ref=binding.bt_order_ref,
                instrument=intent.instrument_id,
                side="BUY",
                requested_qty=intent.integer_qty,
                reference_price=float(intent.reference_price),
                status=(CallbackStatus.ACCEPTED if record.state is LocalOrderState.ACKNOWLEDGED
                        else CallbackStatus.PARTIALLY_FILLED),
                filled_qty=fill_qty,
                average_fill_price=(float(record.average_fill_price)
                                    if record.average_fill_price is not None else None),
                cumulative_commission=0.0,
                occurred_at=occurred_at,
                position_size_after=position_size_after,
                position_average_price_after=position_average_price_after,
                fills=executions,
            ))
        return states

    def _reject_pending_callback_race(self) -> None:
        if self.runtime.order_index.pending_inbox() or self.runtime.order_index.pending_callbacks():
            raise ManagedPythonPaperSessionError(
                "callback arrived during native broker attachment; reconcile before starting Cerebro"
            )

    def push_completed_bar(self, bar: Any) -> None:
        with self._lock:
            if not self._started or self._finished:
                raise ManagedPythonPaperSessionError("session is not accepting completed bars")
            state, _ = self.runtime.service.state()
            if state is not ExecutionControlState.RUNNING:
                raise ExecutionServiceNotReady("managed PAPER session accepts bars only while explicitly RUNNING")
            value_instrument = getattr(bar, "instrument_id", None)
            feed = self.feeds.get(value_instrument)
            if feed is None:
                raise ManagedPythonPaperSessionError("bar instrument is outside the frozen strategy feed set")
            feed.push_completed_bar(bar)

    def pause(self, *, reason: str = "strategy_pause") -> None:
        """Block new bars/orders while keeping Cerebro and callback worker alive."""
        with self._lock:
            self.runtime.pause(reason=reason)

    def cancel_owned_order(self, intent_id: str):
        """Delegate one exact durable intent cancellation to the managed runtime.

        The runtime revalidates account scope, the persisted intent and broker
        order binding, SDK health, grant-produced ledger state and uncertain
        send behavior. The session does not accept broker order IDs or build a
        separate cancellation path.
        """
        return self.runtime.cancel_owned_order(intent_id)

    @property
    def exception(self) -> BaseException | None:
        with self._lock:
            return self._exception

    @property
    def health_error_code(self) -> str | None:
        """Return a stable diagnostic without exposing exception contents."""
        with self._lock:
            return (
                "managed_python_session_failed"
                if self._exception is not None and not self._recovery_ready else None
            )

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, *, timeout: float = 10.0) -> bool:
        """Stop only with no queued bars, active orders, or pending callbacks."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        with self._lock:
            if not self._started:
                if self.runtime.service.lease_held:
                    # Recovery can fail before the session becomes started.
                    # Runtime.stop owns quiescence and can safely transition
                    # RECOVERING to STOPPING; pause only accepts PAUSED/RUNNING.
                    return self.runtime.stop()
                raise ManagedPythonPaperSessionError("session has not acquired a runtime owner lease")
            if self._finished:
                raise ManagedPythonPaperSessionError("session is already stopping or stopped")
            if not self._stopping and any(feed.pending_bar_count for feed in self.feeds.values()):
                raise ManagedPythonPaperSessionError("queued completed bars must be processed before stopping")
            self.runtime.pause(reason="strategy_session_stop")
            if not self._stopping:
                try:
                    # Stop is a distinct control action: freeze both the
                    # strategy cursor and the send gate before deciding which
                    # durable orders can safely remain unattached. A single
                    # exact committed CREATED intent has no broker order to
                    # cancel, so it may remain visible in the ledger while the
                    # runtime shuts down. It is never sent by this path.
                    self.journal.pause_run(self.run_id)
                    pending_created = self._committed_created_recovery_candidates()
                    self._reject_unrestorable_recovery_state(
                        allowed_created_intent_ids={item["intent_id"] for item in pending_created},
                    )
                except ManagedPythonPaperSessionError:
                    return False
                self._stopping = True
                for feed in self.feeds.values():
                    feed.stop_feed()
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            raise ManagedPythonPaperSessionError("session thread is unavailable or cannot join itself")
        thread.join(timeout)
        if thread.is_alive() or (self._exception is not None and not self._recovery_ready):
            return False
        if self.runtime.order_index.pending_inbox() or self.runtime.order_index.pending_callbacks():
            return False
        if not self.runtime.stop():
            return False
        with self._lock:
            self._finished = True
        return True

    def _run_cerebro(self) -> None:
        try:
            assert self.cerebro is not None
            strategies = self.cerebro.run()
            if len(strategies) != 1:
                raise ManagedPythonPaperSessionError("Cerebro did not return exactly one strategy session")
            self.strategy = strategies[0]
            failures = [feed.failure for feed in self.feeds.values() if feed.failure is not None]
            if failures:
                raise failures[0]
        except BaseException as exc:
            with self._lock:
                self._exception = exc
                self._recovery_ready = False
            # Persist the run-level gate first. This is idempotent and scoped
            # to this run, so even failures while the execution service is
            # already paused cannot leave a RUNNING journal behind.
            try:
                self.journal.pause_run(self.run_id)
            except Exception:
                pass
            try:
                state, _ = self.runtime.service.state()
                if state is ExecutionControlState.RUNNING and self.runtime.service.lease_held:
                    self.runtime.pause(reason="managed_python_strategy_failure")
            except Exception:
                pass
            # Cerebro is gone, so its PortBroker queue has no consumer. Keep
            # the independent callback worker alive for ledger updates and
            # management controls, but stop appending transient notifications
            # to a dead broker. The callback inbox remains unacknowledged and
            # is replayed into the replacement broker during explicit recovery.
            worker = self.runtime.callback_worker
            if worker.alive and worker.failure is None:
                try:
                    if worker.pause(timeout=self.runtime.stop_timeout):
                        pending_ids = self.runtime.order_index.pending_delivery_event_ids()
                        worker.rebind_delivery_sink(
                            lambda _callback: None,
                            expected_pending_event_ids=pending_ids,
                        )
                        worker.resume()
                except Exception:
                    # Failure to detach cannot make the failed strategy safe
                    # to resume; preserve the original exception and the
                    # runtime's already-closed dispatch gate.
                    pass


__all__ = ["CallbackSinkRouter", "ManagedPythonPaperSession", "ManagedPythonPaperSessionError"]
