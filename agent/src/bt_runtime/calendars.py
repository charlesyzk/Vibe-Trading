"""Version-pinned exchange session schedules for trusted market-data imports.

The calendar supplies session boundaries only. It does not authenticate a
vendor's bars, symbol mapping, adjustment policy, or point-in-time availability.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from src.bt_runtime.market_data import MarketSession


CALENDAR_ID = "XNYS"
EXPECTED_CALENDAR_VERSION = "4.13.2"
CALENDAR_TIMEZONE = "America/New_York"
REGULAR_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class SessionSchedule:
    calendar_id: str
    calendar_version: str
    start_date: date
    end_date: date
    sessions: tuple[MarketSession, ...]
    sha256: str


def build_xnys_schedule(start: date, end: date) -> SessionSchedule:
    """Build a deterministic XNYS regular-session schedule for an inclusive range.

    The resolved package version is checked at runtime so a locally installed
    calendar cannot silently disagree with the version recorded in manifests.
    """
    if not isinstance(start, date) or isinstance(start, datetime):
        raise TypeError("start must be a date")
    if not isinstance(end, date) or isinstance(end, datetime):
        raise TypeError("end must be a date")
    if end < start:
        raise ValueError("end must be on or after start")
    version = importlib.metadata.version("exchange-calendars")
    if version != EXPECTED_CALENDAR_VERSION:
        raise RuntimeError(
            f"XNYS calendar requires exchange-calendars {EXPECTED_CALENDAR_VERSION}; found {version}"
        )

    calendar = xcals.get_calendar(
        CALENDAR_ID,
        # Include neighboring scheduled sessions even when the requested
        # boundary itself falls on a weekend or exchange holiday.
        start=pd.Timestamp(start.isoformat()) - pd.Timedelta(days=14),
        end=pd.Timestamp(end.isoformat()) + pd.Timedelta(days=14),
    )
    labels = calendar.sessions_in_range(pd.Timestamp(start.isoformat()), pd.Timestamp(end.isoformat()))
    sessions: list[MarketSession] = []
    for label in labels:
        session_day = label.date()
        opened = calendar.session_open(label).to_pydatetime().astimezone(timezone.utc)
        closed = calendar.session_close(label).to_pydatetime().astimezone(timezone.utc)
        local_close = closed.astimezone(ZoneInfo(CALENDAR_TIMEZONE)).time()
        sessions.append(
            MarketSession(
                session_date=session_day,
                open_at=opened,
                close_at=closed,
                early_close=local_close < REGULAR_CLOSE,
            )
        )
    canonical = [
        {
            "session_date": row.session_date.isoformat(),
            "open_at": row.open_at.isoformat(),
            "close_at": row.close_at.isoformat(),
            "early_close": row.early_close,
        }
        for row in sessions
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return SessionSchedule(
        calendar_id=CALENDAR_ID,
        calendar_version=version,
        start_date=start,
        end_date=end,
        sessions=tuple(sessions),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


@lru_cache(maxsize=512)
def resolve_xnys_session(session_date: date) -> MarketSession | None:
    """Resolve one scheduled XNYS session from the pinned calendar package."""
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise TypeError("session_date must be a date")
    schedule = build_xnys_schedule(session_date, session_date)
    return schedule.sessions[0] if schedule.sessions else None
