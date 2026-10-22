"""Trusted one-event-at-a-time bridge from verified bars to an isolated worker.

The coordinator owns event sequencing, strategy source lookup, checkpoints,
and atomic result/outbox commits. The worker receives one current bar plus a
JSON checkpoint; it never receives a DataFrame, file path, account identity,
or the host's next-event cursor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from src.execution.contracts import Bar, SourceKind, canonical_json
from src.execution.event_journal import (
    EventNotReady,
    ReleasedEvent,
    SQLiteEventJournal,
    WorkerEventResult,
)
from src.execution.python_sandbox import PythonSandbox, SandboxStrategyFailure
from src.execution.strategy_source_policy import StrategySourcePolicyError, validate_strategy_source
from src.execution.versions import StrategyVersionStore

PYTHON_WORKER_COMPILER_VERSION = "vibe-python-event/1"
STRATEGY_IPC_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class EventWorkerOutcome:
    event: ReleasedEvent
    committed: bool
    result: WorkerEventResult


class EventWorkerError(RuntimeError):
    """A fail-closed strategy worker or event contract failure."""

    def __init__(self, message: str, *, public_failure: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.public_failure = public_failure


class PythonEventCoordinator:
    """Release one validated completed bar and atomically commit its result."""

    def __init__(
        self,
        *,
        journal: SQLiteEventJournal,
        versions: StrategyVersionStore,
        sandbox: PythonSandbox,
        now=None,
    ) -> None:
        self.journal = journal
        self.versions = versions
        self.sandbox = sandbox
        self._now = now or (lambda: datetime.now(timezone.utc))
        if not callable(self._now):
            raise TypeError("now must be callable")

    def bind_trusted_clock(self, now) -> None:
        """Use the session's authoritative clock for online bar availability checks."""
        if not callable(now):
            raise TypeError("now must be callable")
        self._now = now

    def process_next(
        self,
        run_id: str,
        *,
        next_bar: Bar | dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> EventWorkerOutcome:
        """Retry an outstanding event, or stage and release exactly ``next_bar``.

        When recovering a released but uncommitted event, ``next_bar`` is
        ignored and the durable staged event is replayed first. This prevents
        a new caller-supplied bar from replacing the authoritative cursor.
        """
        identity = self.journal.get_run_identity(run_id)
        version_hash = identity["strategy_version_hash"]
        record = self.versions.get_version(version_hash)
        if record.source_kind is not SourceKind.PYTHON_UPLOAD:
            raise EventWorkerError("event worker only executes frozen Python upload versions")
        source = self.versions.read_upload_source(version_hash)
        try:
            source_text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EventWorkerError("frozen Python source is not valid UTF-8") from exc
        try:
            validate_strategy_source(source_text)
        except StrategySourcePolicyError as exc:
            raise EventWorkerError("strategy source is outside the supported worker API") from exc

        try:
            released = self.journal.release_next_event(run_id)
        except EventNotReady:
            if next_bar is None:
                raise
            try:
                bar = _completed_bar_payload(next_bar, record.spec, now=self._now())
            except Exception:
                self._pause_invalid_bar(run_id)
                raise
            payload = {"bar": bar.model_dump(mode="json")}
            self.journal.stage_next_event(run_id, payload, event_id=event_id)
            released = self.journal.release_next_event(run_id)

        # Re-validate persisted data at the boundary. This catches a corrupted
        # or incorrectly staged payload before it reaches untrusted code.
        if set(released.payload) != {"bar"}:
            self._pause_invalid_bar(run_id)
            raise EventWorkerError("released worker event must contain exactly one bar")
        try:
            bar = _completed_bar_payload(released.payload["bar"], record.spec, now=self._now())
        except Exception:
            self._pause_invalid_bar(run_id)
            raise
        result = invoke_strategy_worker(
            sandbox=self.sandbox,
            source=source_text,
            event_id=released.event_id,
            bar=bar,
            state=self.journal.get_checkpoint(run_id),
        )
        committed = self.journal.commit_result(run_id, result)
        return EventWorkerOutcome(event=released, committed=committed, result=result)

    def _pause_invalid_bar(self, run_id: str) -> None:
        """Persistently stop a run when its host input violates the frozen bar contract."""
        try:
            self.journal.pause_run(run_id)
        except Exception:
            # Preserve the original validation failure. A journal write failure
            # already prevents releasing or executing this event.
            pass

    def shadow_replay_committed_prefix(self, run_id: str) -> tuple[WorkerEventResult, ...]:
        """Recompute a paused run's committed Python event prefix without side effects.

        This method only reads journal rows and invokes the isolated strategy
        worker. It never releases/commits events or creates outbox rows. Any
        source, event, result, or checkpoint mismatch keeps the run PAUSED.
        """
        try:
            prefix = self.journal.read_committed_prefix(run_id)
            record = self.versions.get_version(prefix.strategy_version_hash)
            if record.source_kind is not SourceKind.PYTHON_UPLOAD:
                raise EventWorkerError("event worker only replays frozen Python upload versions")
            if record.compiler_version != PYTHON_WORKER_COMPILER_VERSION:
                raise EventWorkerError("frozen strategy compiler version is not available for shadow replay")
            source = self.versions.read_upload_source(prefix.strategy_version_hash)
            source_text = source.decode("utf-8", errors="strict")
            validate_strategy_source(source_text)
            state = prefix.initial_checkpoint
            replayed: list[WorkerEventResult] = []
            for event in prefix.events:
                if set(event.payload) != {"bar"}:
                    raise EventWorkerError("committed worker event must contain exactly one bar")
                bar = _completed_bar_payload(event.payload["bar"], record.spec, now=self._now())
                recomputed = invoke_strategy_worker(
                    sandbox=self.sandbox,
                    source=source_text,
                    event_id=event.event_id,
                    bar=bar,
                    state=state,
                )
                if canonical_json(recomputed) != canonical_json(event.result):
                    raise EventWorkerError(f"committed event {event.seq} shadow replay differs")
                state = recomputed.state
                replayed.append(recomputed)
            if canonical_json(state) != canonical_json(prefix.committed_checkpoint):
                raise EventWorkerError("shadow replay checkpoint differs from the durable checkpoint")
            return tuple(replayed)
        except Exception as exc:
            try:
                self.journal.pause_run(run_id)
            except Exception:
                pass
            if isinstance(exc, EventWorkerError):
                raise
            raise EventWorkerError("committed event prefix shadow replay failed") from exc


class PythonBacktestEventCoordinator:
    """Replay-safe event coordinator for offline Cerebro runs.

    It uses a backtest-only journal with no account identity or dispatchable
    outbox. On a restarted backtest, committed results are replayed from bar 1
    into a fresh BackBroker; only the first uncommitted bar invokes the worker.
    """

    def __init__(
        self,
        *,
        journal: Any,
        run_id: str,
        source: str,
        sandbox: PythonSandbox,
        released_bars: Sequence[Bar],
    ) -> None:
        self.journal = journal
        self.run_id = run_id
        self.source = source
        self.sandbox = sandbox
        # Copy once so the per-event API never accepts a caller-selected Bar.
        # The trusted importer/service owns this sequence and binds its content
        # to the run configuration before constructing the coordinator.
        self._released_bars = tuple(released_bars)
        if not self._released_bars:
            raise ValueError("backtest coordinator requires a non-empty verified bar sequence")
        if any(not bar.is_final for bar in self._released_bars):
            raise ValueError("backtest coordinator only accepts completed verified bars")
        if any(
            current.available_at <= previous.available_at
            for previous, current in zip(self._released_bars, self._released_bars[1:])
        ):
            raise ValueError("verified bars must be strictly increasing by availability time")
        self.reused_results = 0
        self.computed_results = 0

    def process_event(self, seq: int, *, feed_available_at: datetime) -> WorkerEventResult:
        """Release the verified bar matching the current Backtrader feed point.

        Callers provide the feed timestamp observed by the trusted Backtrader
        shell, never a Bar. The timestamp and durable journal cursor must both
        match the coordinator-owned verified sequence before worker execution.
        """
        if type(seq) is not int or not 1 <= seq <= len(self._released_bars):
            # A malformed/out-of-range host cursor means this Cerebro run no
            # longer agrees with the immutable verified bar sequence. Keep
            # this integrity failure durable, just like a timestamp mismatch
            # or a skipped journal cursor, so a later valid-looking call
            # cannot resume the same run.
            self.journal.mark_paused(self.run_id)
            raise EventWorkerError("event sequence is outside the verified backtest bar range")
        # A datetime subclass can override utcoffset()/astimezone() and return
        # an invalid value after the basic type check. Backtrader supplies a
        # plain datetime, so reject subclasses at this trust boundary.
        if type(feed_available_at) is not datetime:
            self.journal.mark_paused(self.run_id)
            raise EventWorkerError("Backtrader feed timestamp must be a timezone-aware datetime")
        try:
            is_timezone_aware = (
                feed_available_at.tzinfo is not None
                and feed_available_at.utcoffset() is not None
            )
            feed_time_utc = feed_available_at.astimezone(timezone.utc)
            if (
                type(feed_time_utc) is not datetime
                or feed_time_utc.tzinfo is None
                or feed_time_utc.utcoffset() is None
            ):
                raise ValueError("normalized feed timestamp is not a timezone-aware datetime")
            bar = self._released_bars[seq - 1]
            timestamp_delta = abs(feed_time_utc - bar.available_at)
        except Exception as exc:
            self.journal.mark_paused(self.run_id)
            raise EventWorkerError("Backtrader feed timestamp could not be normalized") from exc
        if not is_timezone_aware:
            self.journal.mark_paused(self.run_id)
            raise EventWorkerError("Backtrader feed timestamp must be timezone-aware")
        # Backtrader's float-day conversion can round a timezone-aware source
        # timestamp by a few microseconds. Bound that representation error
        # tightly; this is far below any supported daily-bar time interval.
        if timestamp_delta > timedelta(microseconds=50):
            self.journal.mark_paused(self.run_id)
            raise EventWorkerError("Backtrader feed timestamp does not match the verified bar cursor")
        result, reused = self.journal.get_or_process_event(
            run_id=self.run_id,
            seq=seq,
            event_id=f"{self.run_id}:{seq}",
            bar=bar,
            source=self.source,
            sandbox=self.sandbox,
        )
        if reused:
            self.reused_results += 1
        else:
            self.computed_results += 1
        return result


def invoke_strategy_worker(
    *, sandbox: PythonSandbox, source: str, event_id: str, bar: Bar, state: dict[str, Any]
) -> WorkerEventResult:
    """Execute one event in the isolated image and validate its untrusted result."""
    request = {
        "op": "strategy_event",
        "protocol_version": STRATEGY_IPC_PROTOCOL_VERSION,
        "request_id": uuid.uuid4().hex,
        "source": source,
        "event_id": event_id,
        "event": {"bar": bar.model_dump(mode="json")},
        "state": state,
    }
    try:
        response = sandbox.run_json(request).response
        if not isinstance(response, dict) or set(response) != {
            "protocol_version", "request_id", "event_id", "state", "order_proposals"
        }:
            raise EventWorkerError("worker response has an invalid shape")
        if type(response["protocol_version"]) is not int or response["protocol_version"] != STRATEGY_IPC_PROTOCOL_VERSION:
            raise EventWorkerError("worker response protocol version does not match")
        if response["request_id"] != request["request_id"]:
            raise EventWorkerError("worker response request_id does not match the outstanding IPC request")
        result = WorkerEventResult.model_validate({
            "event_id": response["event_id"],
            "state": response["state"],
            "order_proposals": response["order_proposals"],
        })
        if result.event_id != event_id:
            raise EventWorkerError("worker response event_id does not match the released event")
        return result
    except SandboxStrategyFailure as exc:
        failure = {
            "category": exc.category,
            "error_type": exc.error_type,
            "source_line": exc.source_line,
        }
        raise EventWorkerError("strategy worker reported a normalized strategy failure", public_failure=failure) from exc
    except EventWorkerError:
        raise
    except Exception as exc:
        raise EventWorkerError(f"strategy worker result was rejected: {exc}") from exc


def _completed_bar_payload(value: Bar | dict[str, Any], spec: Any, *, now: datetime) -> Bar:
    """Validate a daily bar against the frozen V1 US equity strategy contract."""
    try:
        bar = value if isinstance(value, Bar) else Bar.model_validate(value)
    except Exception as exc:
        raise EventWorkerError("strategy input is not a valid completed-bar contract") from exc
    if not bar.is_final:
        raise EventWorkerError("only completed bars can be staged for strategy execution")
    if bar.instrument_id != spec.instrument_ids[0]:
        raise EventWorkerError("bar instrument differs from the frozen strategy version")
    if bar.currency != "USD":
        raise EventWorkerError("V1 strategy bars must use the frozen USD instrument contract")
    if bar.timezone != spec.bar_policy.timezone:
        raise EventWorkerError("bar timezone differs from the frozen strategy version")
    if (
        spec.bar_policy.calendar_id != "us_equity_rth"
        or spec.bar_policy.timezone != "America/New_York"
        or spec.bar_policy.session != "regular"
        or spec.bar_policy.timeframe != "1d"
        or spec.bar_policy.completed_only is not True
    ):
        raise EventWorkerError("frozen strategy version has an unsupported daily-bar policy")
    try:
        from src.bt_runtime.calendars import resolve_xnys_session

        session = resolve_xnys_session(bar.session_date)
    except Exception as exc:
        raise EventWorkerError("pinned XNYS session lookup failed for strategy input") from exc
    if session is None:
        raise EventWorkerError("bar date is not a scheduled XNYS regular session")
    if bar.start_at != session.open_at or bar.end_at != session.close_at:
        raise EventWorkerError("bar timestamps do not match the pinned XNYS regular session")
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise EventWorkerError("trusted strategy clock must return an aware datetime")
    try:
        current = now.astimezone(timezone.utc)
    except Exception as exc:
        raise EventWorkerError("trusted strategy clock could not be normalized") from exc
    if bar.available_at > current:
        raise EventWorkerError("bar is not yet available to the trusted strategy coordinator")
    return bar
