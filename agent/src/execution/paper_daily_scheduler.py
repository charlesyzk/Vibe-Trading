"""Deliver newly completed PAPER daily bars in their next trading session."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from src.bt_runtime.calendars import build_xnys_schedule
from src.execution.order_ledger import ExecutionControlState


class PaperDailyScheduleError(RuntimeError):
    """A bar or schedule cannot be advanced without reconciliation."""


class PaperDailyScheduler:
    """Poll one pinned session; callers own the background thread lifecycle."""

    def __init__(self, *, manager, scope, version_id: str, initial_bar, source: Callable,
                 valid_from: datetime, valid_until: datetime, now: Callable,
                 is_current: Callable[[], bool]):
        self.manager, self.scope, self.version_id = manager, scope, version_id
        self.cursor = initial_bar.session_date
        self.instrument_id = initial_bar.instrument_id
        self.source, self.valid_from, self.valid_until = source, valid_from, valid_until
        self.now, self.is_current = now, is_current
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, interval_seconds: float = 30.0) -> None:
        if interval_seconds <= 0 or self._thread is not None:
            raise ValueError("scheduler interval must be positive and start only once")
        self._thread = threading.Thread(target=self._run, args=(interval_seconds,),
                                        name="paper-daily-scheduler", daemon=True)
        self._thread.start()

    def _run(self, interval_seconds: float) -> None:
        while not self._stop.wait(interval_seconds):
            try:
                if not self.is_current():
                    return
                status = self.manager.status()
                if status is None or status.control_state is not ExecutionControlState.RUNNING:
                    return
                self.tick()
            except Exception:
                # tick closes the send gate when possible; also handle a
                # failure while checking scheduler/session state itself.
                try:
                    if self.is_current():
                        try:
                            status = self.manager.status()
                        except Exception:
                            status = None
                        if status is None or status.control_state is ExecutionControlState.RUNNING:
                            self.manager.pause(self.scope, reason="paper_daily_schedule_failed")
                except Exception:
                    pass
                return

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> bool:
        """Feed at most one immediately preceding completed session, or wait."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self.is_current():
                return False
            status = self.manager.status()
            if status is None or status.control_state is not ExecutionControlState.RUNNING:
                return False
            now = self._utc_now()
            if now >= self.valid_until:
                self.manager.pause(self.scope, reason="paper_entry_window_expired")
                return False
            if now < self.valid_from:
                return False
            today = now.astimezone(ZoneInfo("America/New_York")).date()
            if today < self.cursor:
                raise PaperDailyScheduleError("PAPER daily cursor is in the future")
            sessions = build_xnys_schedule(self.cursor, today).sessions
            if not sessions or sessions[0].session_date != self.cursor:
                raise PaperDailyScheduleError("PAPER daily cursor is not a trading session")
            current = sessions[-1]
            if current.session_date != today or not (current.open_at <= now < current.close_at):
                return False
            previous = sessions[-2] if len(sessions) > 1 else None
            if previous is None or previous.session_date == self.cursor:
                return False
            if len(sessions) != 3:
                raise PaperDailyScheduleError("missed PAPER sessions require a new reconciliation")
            bars = self.source(after_session=self.cursor)
            if len(bars) != 1 or bars[0].session_date != previous.session_date or bars[0].instrument_id != self.instrument_id:
                raise PaperDailyScheduleError("latest PAPER daily bar is unavailable or mismatched")
            observed = self._utc_now()
            if not (self.valid_from <= observed < self.valid_until and current.open_at <= observed < current.close_at):
                raise PaperDailyScheduleError("PAPER entry window closed while loading the bar")
            self.manager.push_scheduled_completed_bar(
                self.scope, expected_strategy_version_id=self.version_id, bar=bars[0], is_current=self.is_current,
            )
            self.cursor = bars[0].session_date
            return True
        except Exception:
            if self.is_current():
                status = self.manager.status()
                if status is not None and status.control_state is ExecutionControlState.RUNNING:
                    self.manager.pause(self.scope, reason="paper_daily_schedule_failed")
            raise
        finally:
            self._lock.release()

    def _utc_now(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PaperDailyScheduleError("PAPER daily clock must include a timezone")
        return value.astimezone(timezone.utc)
