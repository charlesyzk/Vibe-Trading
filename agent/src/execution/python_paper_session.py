"""Explicit offline composition for one frozen Python PAPER strategy version.

This primitive composes host-pushed completed bars with a real Backtrader
``Cerebro``/``PortBroker`` run and the existing durable event/dispatch seam.
The Python strategy source is executed by ``PythonEventCoordinator`` in its
isolated one-event worker; this module installs only a trusted Backtrader shell
that forwards each exact completed ``Bar`` to that bridge. It accepts only an
explicitly marked offline transport and never owns an IBKR process, API app,
or connection configuration.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable

import backtrader as bt

from src.bt_runtime.brokers.proxy import PortBroker
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.backtrader_dispatch import BacktraderDispatchPort, OneShotLedgerIntentResolver
from src.execution.contracts import Environment, PositionMode, SourceKind
from src.execution.dispatch_service import PaperOrderDispatchService
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import PythonEventCoordinator
from src.execution.execution_service import ExecutionServiceNotReady, PaperExecutionService
from src.execution.ibkr_callback_pump import IbkrCallbackPump
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.outbox_dispatch import PythonOutboxIntentHandoff
from src.execution.paper_event_bridge import PythonPaperEventBridge
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


class PythonPaperSessionError(RuntimeError):
    """The requested frozen strategy session cannot be composed safely."""


class _SessionStrategy(bt.Strategy):
    """Trusted BT shell that forwards exact current Bars to the host bridge."""

    params = (("bridge", None), ("initial_last_bar_end", None))

    def __init__(self) -> None:
        self.results: list[Any] = []
        self._last_bar_end = self.p.initial_last_bar_end

    def next(self) -> None:
        bar = getattr(self.data, "current_completed_bar", None)
        if bar is None:
            raise PythonPaperSessionError("Backtrader feed has no trusted current completed bar")
        if bar.end_at == self._last_bar_end:
            return
        if self._last_bar_end is not None and bar.end_at < self._last_bar_end:
            raise PythonPaperSessionError("completed bar time moved backwards")
        result = self.p.bridge.process_completed_bar(self, self.data, bar)
        self.results.append(result)
        self._last_bar_end = bar.end_at


class PythonPaperSession:
    """A single-use, offline Cerebro session pinned to one stored version.

    Lower-level trusted collaborators (risk provider, dispatch service,
    journal, ledger, and callback pump) are injected so this factory does not
    invent account facts or risk policy. ``transport`` and an optional cancel
    transport must opt in with ``offline = True``; a broker SDK transport is
    rejected at this boundary.
    """

    def __init__(
        self,
        *,
        run_id: str,
        version_store: StrategyVersionStore,
        version_id: str,
        journal: SQLiteEventJournal,
        ledger: SQLiteOrderLedger,
        coordinator: PythonEventCoordinator,
        handoff: PythonOutboxIntentHandoff,
        execution_service: PaperExecutionService,
        dispatch: PaperOrderDispatchService,
        callback_pump: IbkrCallbackPump,
        resolve_contract_identity: Callable,
        allocate_order_id: Callable[[], int],
        observe_next_valid_id: Callable[[int], None],
        transport: Callable,
        cancel_transport: Callable[[int], None] | None = None,
        initial_cash: float = 100_000.0,
        now: Callable | None = None,
        queue_size: int = 16,
        idle_poll_seconds: float = 0.05,
    ) -> None:
        self.record = self._validate_composition(
            run_id=run_id,
            version_store=version_store,
            version_id=version_id,
            journal=journal,
            ledger=ledger,
            coordinator=coordinator,
            handoff=handoff,
            execution_service=execution_service,
            dispatch=dispatch,
            callback_pump=callback_pump,
            transport=transport,
            cancel_transport=cancel_transport,
            initial_cash=initial_cash,
            queue_size=queue_size,
        )
        if not callable(transport) or (cancel_transport is not None and not callable(cancel_transport)):
            raise TypeError("offline order transports must be callable")
        if not callable(resolve_contract_identity):
            raise TypeError("resolve_contract_identity must be callable")
        if not callable(allocate_order_id) or not callable(observe_next_valid_id):
            raise TypeError("order-id allocator and observer must be callable")

        self.run_id = run_id
        self.version_id = version_id
        self.version_hash = self.record.version_hash
        self.version_store = version_store
        self.journal = journal
        self.ledger = ledger
        self.execution_service = execution_service
        self.callback_pump = callback_pump
        self.now = now or (lambda: datetime.now(timezone.utc))
        coordinator.bind_trusted_clock(self.now)
        self.intent_resolver = OneShotLedgerIntentResolver(ledger)
        owner_lease = execution_service.require_owner_lease()
        self.port = BacktraderDispatchPort(
            ledger=ledger,
            dispatch=dispatch,
            callback_pump=callback_pump,
            resolve_intent=self.intent_resolver,
            resolve_contract_identity=resolve_contract_identity,
            owner_lease=owner_lease,
            allocate_order_id=allocate_order_id,
            observe_next_valid_id=observe_next_valid_id,
            transport=transport,
            cancel_transport=cancel_transport,
            now=self.now,
        )
        self.bridge = PythonPaperEventBridge(
            run_id=run_id,
            journal=journal,
            ledger=ledger,
            coordinator=coordinator,
            handoff=handoff,
            execution_service=execution_service,
            intent_resolver=self.intent_resolver,
        )
        spec = self.record.spec
        self.feed = PaperCompletedBarFeed(
            instrument_id=spec.instrument_ids[0],
            timezone=spec.bar_policy.timezone,
            currency="USD",
            queue_size=queue_size,
            idle_poll_seconds=idle_poll_seconds,
            now=self.now,
            failure_sink=lambda _exc: self._pause_on_feed_failure(),
        )
        self.broker = PortBroker(self.port, initial_cash=float(initial_cash))
        self.cerebro = bt.Cerebro(
            stdstats=False,
            preload=False,
            runonce=False,
            quicknotify=True,
        )
        self.cerebro.adddata(self.feed, name=spec.instrument_ids[0])
        self.cerebro.setbroker(self.broker)
        self.cerebro.addstrategy(_SessionStrategy, bridge=self.bridge)
        self.strategy: _SessionStrategy | None = None
        self._started = False
        self._finished = False
        self._exception: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()

    @classmethod
    def _validate_composition(
        cls,
        *,
        run_id: str,
        version_store: StrategyVersionStore,
        version_id: str,
        journal: SQLiteEventJournal,
        ledger: SQLiteOrderLedger,
        coordinator: PythonEventCoordinator,
        handoff: PythonOutboxIntentHandoff,
        execution_service: PaperExecutionService,
        dispatch: PaperOrderDispatchService,
        callback_pump: IbkrCallbackPump,
        transport: Callable,
        cancel_transport: Callable | None,
        initial_cash: float,
        queue_size: int,
    ) -> StrategyVersionRecord:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id is required")
        if not isinstance(version_store, StrategyVersionStore):
            raise TypeError("version_store must be a StrategyVersionStore")
        record = version_store.get_version(version_id)
        if record.source_kind is not SourceKind.PYTHON_UPLOAD:
            raise PythonPaperSessionError("Python paper session requires a frozen Python upload version")
        if record.spec.position_policy.mode is not PositionMode.EXPLICIT_QUANTITY:
            raise PythonPaperSessionError("Python paper session requires explicit whole-share quantities")
        if record.spec.execution_policy.outside_rth:
            raise PythonPaperSessionError("outside-RTH execution is unsupported")
        if not isinstance(journal, SQLiteEventJournal) or not isinstance(ledger, SQLiteOrderLedger):
            raise TypeError("journal and ledger must be the trusted SQLite stores")
        if not isinstance(coordinator, PythonEventCoordinator):
            raise TypeError("coordinator must be a PythonEventCoordinator")
        if not isinstance(handoff, PythonOutboxIntentHandoff):
            raise TypeError("handoff must be a PythonOutboxIntentHandoff")
        if not isinstance(execution_service, PaperExecutionService):
            raise TypeError("execution_service must be a PaperExecutionService")
        if not isinstance(dispatch, PaperOrderDispatchService):
            raise TypeError("dispatch must be a PaperOrderDispatchService")
        if not isinstance(callback_pump, IbkrCallbackPump):
            raise TypeError("callback_pump must be an IbkrCallbackPump")
        if (
            coordinator.versions is not version_store
            or coordinator.journal is not journal
            or handoff.journal is not journal
            or handoff.ledger is not ledger
            or dispatch.ledger is not ledger
            or callback_pump.ledger is not ledger
        ):
            raise PythonPaperSessionError("session collaborators do not share the same trusted stores")
        scope = execution_service
        if scope.broker != "IBKR" or scope.environment is not Environment.PAPER:
            raise PythonPaperSessionError("session requires the IBKR PAPER execution scope")
        canonical_ledger = Path(ledger.path).expanduser().resolve()
        if (
            Path(dispatch.ledger.path).expanduser().resolve() != canonical_ledger
            or Path(callback_pump.order_index.path).expanduser().resolve() != canonical_ledger
        ):
            raise PythonPaperSessionError("ledger, callback index, and dispatch must share one database")
        identity = journal.get_run_identity(run_id)
        if (
            identity["strategy_version_hash"] != record.version_hash
            or identity["account_ref"] != scope.account_ref
            or identity["environment"] != Environment.PAPER.value
        ):
            raise PythonPaperSessionError("journal run identity differs from the frozen PAPER session")
        if not scope.lease_held or scope.state()[0].value != "RUNNING":
            raise ExecutionServiceNotReady("offline session requires an explicitly RUNNING PAPER service")
        if getattr(transport, "offline", False) is not True:
            raise PythonPaperSessionError("offline session requires a transport marked offline=True")
        if cancel_transport is not None and getattr(cancel_transport, "offline", False) is not True:
            raise PythonPaperSessionError("offline cancel transport must be marked offline=True")
        if isinstance(initial_cash, bool) or not isinstance(initial_cash, (int, float)) or not math.isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("initial_cash must be finite and non-negative")
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        return record

    def push_completed_bar(self, bar: Any) -> None:
        """Push one host-verified immutable bar into the fixed strategy feed."""
        with self._lifecycle_lock:
            if self._finished:
                raise PythonPaperSessionError("bars cannot be pushed after session input is finished")
            self.feed.push_completed_bar(bar)

    def finish_input(self) -> None:
        """Close input and let Cerebro drain queued bars before returning."""
        with self._lifecycle_lock:
            if self._finished:
                return
            self._finished = True
            self.feed.stop_feed()

    def stop(self) -> None:
        """Signal the running feed to drain and finish; equivalent to finish_input."""
        self.finish_input()

    @property
    def exception(self) -> BaseException | None:
        """Return the captured Cerebro-thread exception, if one occurred."""
        with self._lifecycle_lock:
            return self._exception

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the real Cerebro loop on a host-owned thread exactly once.

        The host may continue pushing verified bars until ``finish_input`` or
        ``stop`` is called. Use ``join`` to wait and inspect ``exception`` for
        any error raised on the Cerebro thread.
        """
        with self._lifecycle_lock:
            if self._started:
                raise PythonPaperSessionError("session can only start once")
            self._started = True
            self._thread = threading.Thread(
                target=self._run_cerebro,
                name=f"python-paper-cerebro-{self.run_id}",
            )
            self._thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for Cerebro completion; return False if timeout elapsed."""
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            raise PythonPaperSessionError("session has not been started")
        if thread is threading.current_thread():
            raise PythonPaperSessionError("Cerebro thread cannot join itself")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout < 0
        ):
            raise ValueError("join timeout must be finite and non-negative")
        thread.join(timeout)
        return not thread.is_alive()

    def run(self, *, finish_input: bool = True) -> _SessionStrategy:
        """Run the real Cerebro loop synchronously once; no broker is launched."""
        if finish_input:
            self.finish_input()
        self.start()
        self.join()
        if self.exception is not None:
            raise self.exception
        assert self.strategy is not None
        return self.strategy

    def _run_cerebro(self) -> None:
        try:
            strategies = self.cerebro.run()
            if len(strategies) != 1:
                raise PythonPaperSessionError("Cerebro did not return exactly one strategy session")
            self.strategy = strategies[0]
            if self.feed.failure is not None:
                raise self.feed.failure
        except BaseException as exc:
            with self._lifecycle_lock:
                self._exception = exc
            self._pause_after_runtime_failure()

    def _pause_after_runtime_failure(self) -> None:
        try:
            state, _ = self.execution_service.state()
            if state.value == "RUNNING" and self.execution_service.lease_held:
                self.execution_service.pause(reason="paper_strategy_runtime_failure")
        except Exception:
            # Captured exception remains available to the host even when pause
            # persistence itself fails.
            pass

    def _pause_on_feed_failure(self) -> None:
        try:
            state, _ = self.execution_service.state()
            if state.value == "RUNNING" and self.execution_service.lease_held:
                self.execution_service.pause(reason="paper_completed_feed_failure")
        except Exception:
            # Feed failure remains latched and visible even if durable pause
            # itself fails; dispatch also checks the persistent control gate.
            pass


__all__ = ["PythonPaperSession", "PythonPaperSessionError"]
