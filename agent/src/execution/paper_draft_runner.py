"""Start a saved PAPER draft and schedule subsequent completed signals."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.bt_runtime.calendars import build_xnys_schedule, resolve_xnys_session
from src.execution.paper_order_draft import load_paper_order_draft
from src.execution.paper_order_preparation import PaperOrderSetup
from src.execution.paper_daily_scheduler import PaperDailyScheduler
from src.execution.paper_strategy_warmup import PaperWarmup, verify_current_signal_bar
from src.session.models import Principal


class PaperDraftRunError(ValueError):
    """A safe, actionable draft-start rejection."""


class PaperDraftRunner:
    """Start the pinned session and supervise its daily signal source."""

    def __init__(self, *, manager, scope, runtime_root: Path, draft: dict, warmup: PaperWarmup,
                 source: Callable, now: Callable[[], datetime]):
        self.manager, self.scope, self.runtime_root = manager, scope, runtime_root
        self.draft, self.warmup, self.source, self.now = draft, warmup, source, now
        self._lock = threading.Lock()
        self._scheduler: PaperDailyScheduler | None = None

    def __call__(self, principal: Principal, *, is_current: Callable[[], bool]) -> dict:
        if not isinstance(principal, Principal) or not principal.paper_operator:
            raise PermissionError("an attributable operator is required")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a PAPER action is already in progress")
        try:
            if not is_current():
                raise RuntimeError("PAPER deployment changed")
            self._check_draft_and_window()
            bars = self.source()
            try:
                bar = verify_current_signal_bar(signal_bar=self.warmup.signal_bar, gateway_bars=bars)
            except ValueError as exc:
                raise PaperDraftRunError(str(exc)) from exc
            observed_at = self.now()
            sessions = build_xnys_schedule(bar.session_date, observed_at.date()).sessions
            if any(session.session_date > bar.session_date and session.close_at <= observed_at for session in sessions):
                raise PaperDraftRunError("backtest signal is not the latest completed market session")
            # The network read can take long enough for the window or saved draft to change.
            self._check_draft_and_window()
            if not is_current():
                raise RuntimeError("PAPER deployment changed")
            status = self.manager.start_activate_and_push_completed_bar(
                self.scope, principal, expected_strategy_version_id=self.draft["strategy_version_id"], bar=bar,
            )
            try:
                setup = PaperOrderSetup.model_validate(self.draft["setup"])
                token = self.manager.scheduled_session_token(self.scope)
                self._scheduler = PaperDailyScheduler(
                    manager=self.manager, scope=self.scope, version_id=self.draft["strategy_version_id"],
                    initial_bar=bar, source=self.source, valid_from=setup.valid_from, valid_until=setup.valid_until,
                    now=self.now, is_current=lambda: is_current() and self.manager.is_scheduled_session_current(self.scope, token),
                )
                self._scheduler.start()
            except Exception:
                self.manager.pause(self.scope, reason="paper_daily_scheduler_start_failed")
                raise
            return {"action": "paper_draft_initial_signal", "strategy_version_id": self.draft["strategy_version_id"],
                    "bar_session_date": bar.session_date.isoformat(), "phase": status.phase,
                    "signal_enqueued": True, "broker_order_confirmed": False}
        finally:
            self._lock.release()

    def _check_draft_and_window(self) -> None:
        if load_paper_order_draft(runtime_root=self.runtime_root, account_ref=self.scope.account_ref) != self.draft:
            raise PaperDraftRunError("saved PAPER draft changed; deploy it again")
        setup = PaperOrderSetup.model_validate(self.draft["setup"])
        now = self.now()
        if now.tzinfo is None or now.utcoffset() is None or not setup.valid_from <= now < setup.valid_until:
            raise PaperDraftRunError("PAPER entry window is closed")
        session = resolve_xnys_session(now.astimezone(ZoneInfo("America/New_York")).date())
        if session is None or not session.open_at <= now < session.close_at:
            raise PaperDraftRunError("PAPER session can start only during regular trading hours")
        if self.warmup.signal_bar.available_at > now:
            raise PaperDraftRunError("backtest signal is not yet available")
