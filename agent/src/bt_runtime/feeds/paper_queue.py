"""Bounded live-style feed for host-verified completed PAPER bars.

Only the trusted market-data adapter should call ``push_completed_bar``.
Strategy code receives ordinary Backtrader lines and never receives this feed's
queue, provider client, or host cursor. During idle polls ``_load`` returns
``None`` so Cerebro continues broker notification processing without a new bar.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from queue import Empty, Full, Queue
import threading
from typing import Callable
from zoneinfo import ZoneInfo

import backtrader as bt

from src.bt_runtime.calendars import EXPECTED_CALENDAR_VERSION, resolve_xnys_session
from src.execution.contracts import Bar


class PaperFeedError(RuntimeError):
    """The trusted completed-bar boundary rejected data or overflowed."""


_STOP = object()


class PaperCompletedBarFeed(bt.feed.DataBase):
    """A bounded, monotonic completed-bar queue exposed as a live BT feed.

    The class does not fetch market data or certify a provider. Its caller
    must validate provider entitlement, regular-session/calendar completeness,
    contract identity and corrections before pushing each immutable ``Bar``.
    """

    params = (
        ("instrument_id", None),
        ("timezone", "America/New_York"),
        ("currency", "USD"),
        ("calendar_id", "us_equity_rth"),
        ("calendar_version", EXPECTED_CALENDAR_VERSION),
        ("session_resolver", resolve_xnys_session),
        ("queue_size", 16),
        ("idle_poll_seconds", 0.1),
        ("now", None),
        ("failure_sink", None),
    )

    def __init__(self):
        super().__init__()
        if not isinstance(self.p.instrument_id, str) or not self.p.instrument_id.strip():
            raise ValueError("instrument_id is required")
        if self.p.currency != "USD":
            raise ValueError("V1 paper feed only accepts USD instruments")
        if self.p.calendar_id != "us_equity_rth":
            raise ValueError("V1 paper feed requires the frozen us_equity_rth calendar")
        if self.p.calendar_version != EXPECTED_CALENDAR_VERSION:
            raise ValueError(f"V1 paper feed requires XNYS calendar {EXPECTED_CALENDAR_VERSION}")
        if not callable(self.p.session_resolver):
            raise ValueError("session_resolver must be callable")
        if type(self.p.queue_size) is not int or self.p.queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if isinstance(self.p.idle_poll_seconds, bool) or not isinstance(self.p.idle_poll_seconds, (int, float)) or not 0.01 <= self.p.idle_poll_seconds <= 5:
            raise ValueError("idle_poll_seconds must be between 10 ms and 5 seconds")
        try:
            self._zone = ZoneInfo(self.p.timezone)
        except Exception as exc:
            raise ValueError("timezone must be a valid IANA zone") from exc
        self._now: Callable[[], datetime] = self.p.now or (lambda: datetime.now(timezone.utc))
        if not callable(self._now):
            raise TypeError("now must be callable")
        if self.p.failure_sink is not None and not callable(self.p.failure_sink):
            raise TypeError("failure_sink must be callable or None")
        self._queue: Queue[Bar | object] = Queue(maxsize=self.p.queue_size)
        self._lock = threading.Lock()
        self._last_enqueued_session: date | None = None
        self._current_bar: Bar | None = None
        self._closed = False
        self._failure: PaperFeedError | None = None

    def islive(self) -> bool:
        return True

    @property
    def failure(self) -> PaperFeedError | None:
        return self._failure

    @property
    def current_completed_bar(self) -> Bar | None:
        """Return the exact trusted Bar currently exposed as Backtrader lines.

        This is intended for a host-owned strategy adapter. Strategy source
        still receives only ordinary Backtrader lines inside its worker.
        """
        return self._current_bar

    @property
    def pending_bar_count(self) -> int:
        """Return accepted bars not yet consumed by Backtrader."""
        return self._queue.qsize()

    def start(self) -> None:
        super().start()
        self.put_notification(self.LIVE)

    def push_completed_bar(self, value: Bar | dict) -> None:
        """Validate and enqueue one bar released by the trusted host."""
        with self._lock:
            if self._closed:
                raise PaperFeedError("completed bar feed is closed")
            if self._failure is not None:
                raise self._failure
            try:
                bar = value if isinstance(value, Bar) else Bar.model_validate(value)
                self._validate_bar(bar)
                if self._last_enqueued_session is not None and bar.session_date <= self._last_enqueued_session:
                    raise PaperFeedError("bar session dates must be strictly increasing")
                self._queue.put_nowait(bar)
                self._last_enqueued_session = bar.session_date
            except Full as exc:
                raise self._latch_failure("completed bar queue is full") from exc
            except PaperFeedError as exc:
                self._latch_failure(str(exc))
                raise
            except Exception as exc:
                error = self._latch_failure(f"completed bar is invalid: {exc}")
                raise error from exc

    def stop_feed(self) -> None:
        """Wake an idle Cerebro loop and finish the feed after queued bars."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.put_nowait(_STOP)
            except Full:
                # Preserve already accepted bars. The live poll notices the
                # closed state as soon as one queue slot is consumed.
                pass

    def stop(self) -> None:
        self.stop_feed()
        super().stop()

    def _load(self):
        if self._failure is not None:
            raise self._failure
        try:
            item = self._queue.get(timeout=float(self.p.idle_poll_seconds))
        except Empty:
            if self._closed:
                return False
            return None
        if item is _STOP:
            return False
        assert isinstance(item, Bar)
        self._current_bar = item
        stamp = item.end_at.astimezone(self._zone).replace(tzinfo=None)
        self.lines.datetime[0] = bt.date2num(stamp)
        self.lines.open[0] = float(item.open)
        self.lines.high[0] = float(item.high)
        self.lines.low[0] = float(item.low)
        self.lines.close[0] = float(item.close)
        self.lines.volume[0] = float(item.volume)
        self.lines.openinterest[0] = 0.0
        return True

    def _validate_bar(self, bar: Bar) -> None:
        if not bar.is_final:
            raise PaperFeedError("partial bars cannot enter the PAPER strategy feed")
        if bar.instrument_id != self.p.instrument_id:
            raise PaperFeedError("bar instrument differs from the frozen strategy instrument")
        if bar.currency != self.p.currency:
            raise PaperFeedError("bar currency differs from the PAPER feed currency")
        if bar.timezone != self.p.timezone:
            raise PaperFeedError("bar timezone differs from the frozen strategy timezone")
        if self.p.timezone != "America/New_York":
            raise PaperFeedError("us_equity_rth bars must use America/New_York")
        try:
            session = self.p.session_resolver(bar.session_date)
        except Exception as exc:
            raise PaperFeedError(f"trusted XNYS session lookup failed: {exc}") from exc
        if session is None:
            raise PaperFeedError("bar date is not a scheduled XNYS session")
        if bar.start_at.astimezone(timezone.utc) != session.open_at.astimezone(timezone.utc):
            raise PaperFeedError("bar start does not match the scheduled XNYS open")
        if bar.end_at.astimezone(timezone.utc) != session.close_at.astimezone(timezone.utc):
            raise PaperFeedError("bar end does not match the scheduled XNYS close")
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise PaperFeedError("feed clock must return an aware datetime")
        if bar.available_at > current.astimezone(timezone.utc):
            raise PaperFeedError("bar is not yet available to the trusted host")

    def _latch_failure(self, message: str) -> PaperFeedError:
        if self._failure is None:
            self._failure = PaperFeedError(message)
            sink = self.p.failure_sink
            if sink is not None:
                try:
                    sink(self._failure)
                except Exception:
                    pass
        return self._failure
