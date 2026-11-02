"""Read completed RTH daily bars from the configured IBKR PAPER Gateway.

This adapter binds every returned bar to an operator-verified stock contract
and the pinned XNYS session calendar. It does not certify IBKR's adjustment or
point-in-time publication semantics; those remain explicit quality flags.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from src.bt_runtime.calendars import build_xnys_schedule
from src.execution.contracts import Bar
from src.execution.ibkr_paper_risk_facts import VerifiedIbkrContract


class IbkrCompletedBarSourceError(RuntimeError):
    """IBKR history cannot be admitted to the PAPER strategy feed."""


class IbkrPaperCompletedBarSource:
    """Fetch and validate completed SMART/USD/STK regular-session day bars."""

    def __init__(
        self,
        *,
        config: Any,
        account_ref: str,
        contract: VerifiedIbkrContract,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        duration: str = "1 Y",
    ) -> None:
        from src.trading.connectors.ibkr.local import IBKRLocalConfig

        if not isinstance(config, IBKRLocalConfig):
            raise TypeError("config must be IBKRLocalConfig")
        if (config.profile != "paper" or config.readonly is not True
                or config.port not in {4002, 7497}
                or config.host.strip().lower() not in {"localhost", "127.0.0.1", "::1"}
                or config.account != account_ref):
            raise ValueError("bar source requires the exact loopback PAPER read-only account profile")
        if not isinstance(contract, VerifiedIbkrContract):
            raise TypeError("contract must be an operator-verified VerifiedIbkrContract")
        if not callable(now):
            raise TypeError("now must be callable")
        if not isinstance(duration, str) or not re.fullmatch(r"[1-9][0-9]* (D|W|M|Y)", duration):
            raise ValueError("duration must be a positive IBKR daily-history interval")
        self.config = config
        self.account_ref = account_ref
        self.contract = contract
        self.now = now
        self.duration = duration

    def __call__(self, *, after_session: date | None = None) -> tuple[Bar, ...]:
        from src.trading.connectors.ibkr.local import get_historical_bars

        observed_at = self._aware(self.now())
        if after_session is not None:
            if not isinstance(after_session, date) or isinstance(after_session, datetime):
                raise TypeError("after_session must be a date")
            if after_session > observed_at.date():
                raise IbkrCompletedBarSourceError("incremental cursor is in the future")
        try:
            response = get_historical_bars(
                self.contract.symbol,
                config=self.config,
                exchange=self.contract.exchange,
                currency=self.contract.currency,
                sec_type=self.contract.security_type,
                duration=self.duration,
                bar_size="1 day",
                what_to_show="TRADES",
                use_rth=True,
            )
        except Exception as exc:
            raise IbkrCompletedBarSourceError("IBKR PAPER daily history request failed") from exc
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise IbkrCompletedBarSourceError("IBKR PAPER daily history response is invalid")
        self._validate_contract(response.get("qualified_contract"))
        raw_bars = response.get("bars")
        if not isinstance(raw_bars, list):
            raise IbkrCompletedBarSourceError("IBKR PAPER daily history has no bar list")

        parsed: dict[date, Mapping[str, Any]] = {}
        for row in raw_bars:
            if not isinstance(row, Mapping):
                raise IbkrCompletedBarSourceError("IBKR PAPER history contains a malformed bar")
            try:
                session_date = date.fromisoformat(str(row["date"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise IbkrCompletedBarSourceError("IBKR PAPER daily bar date is invalid") from exc
            if session_date in parsed:
                raise IbkrCompletedBarSourceError("IBKR PAPER history contains duplicate session bars")
            parsed[session_date] = row

        complete_dates = []
        for session_date in sorted(parsed):
            session = self._session(session_date)
            if session is None:
                raise IbkrCompletedBarSourceError("IBKR returned a bar for a non-XNYS session")
            if session.close_at <= observed_at:
                complete_dates.append(session_date)

        if after_session is not None:
            complete_dates = [value for value in complete_dates if value > after_session]
            expected_start = after_session
            expected = tuple(
                item.session_date for item in build_xnys_schedule(after_session, observed_at.date()).sessions
                if item.session_date > after_session and item.close_at <= observed_at
            )
            if tuple(complete_dates) != expected:
                raise IbkrCompletedBarSourceError("IBKR history is missing one or more completed XNYS sessions")
        else:
            expected_start = complete_dates[0] if complete_dates else None
        if complete_dates and expected_start is not None:
            expected = tuple(
                item.session_date for item in build_xnys_schedule(expected_start, complete_dates[-1]).sessions
                if item.session_date > expected_start or after_session is None
            )
            if tuple(complete_dates) != expected:
                raise IbkrCompletedBarSourceError("IBKR history is missing one or more completed XNYS sessions")
        return tuple(self._to_bar(parsed[value], value) for value in complete_dates)

    def publish(self, feed: Any, *, after_session: date | None = None) -> int:
        """Push verified completed bars into the matching bounded PAPER feed."""
        from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed

        if not isinstance(feed, PaperCompletedBarFeed):
            raise TypeError("feed must be a PaperCompletedBarFeed")
        if (
            feed.p.instrument_id != self.contract.instrument_id
            or feed.p.currency != self.contract.currency
            or feed.p.timezone != "America/New_York"
            or feed.p.calendar_id != "us_equity_rth"
        ):
            raise IbkrCompletedBarSourceError("completed-bar feed differs from the verified IBKR contract")
        bars = self(after_session=after_session)
        for bar in bars:
            feed.push_completed_bar(bar)
        return len(bars)

    def completed_bar(self, instrument_id: str, session_date: date) -> Bar:
        """Return the exact qualified completed session bar for risk marking."""
        if instrument_id != self.contract.instrument_id:
            raise IbkrCompletedBarSourceError("requested instrument differs from the verified PAPER contract")
        if not isinstance(session_date, date) or isinstance(session_date, datetime):
            raise TypeError("session_date must be a date")
        matches = [bar for bar in self() if bar.session_date == session_date]
        if len(matches) != 1:
            raise IbkrCompletedBarSourceError("requested completed session bar is unavailable")
        return matches[0]

    def _validate_contract(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            raise IbkrCompletedBarSourceError("IBKR history has no qualified contract identity")
        expected = {
            "symbol": self.contract.symbol.upper(),
            "con_id": self.contract.con_id,
            "sec_type": self.contract.security_type.upper(),
            "exchange": self.contract.exchange,
            "primary_exchange": self.contract.primary_exchange,
            "currency": self.contract.currency.upper(),
        }
        actual = {
            "symbol": str(raw.get("symbol", "")).upper(),
            "con_id": raw.get("con_id"),
            "sec_type": str(raw.get("sec_type", "")).upper(),
            "exchange": raw.get("exchange"),
            "primary_exchange": raw.get("primary_exchange"),
            "currency": str(raw.get("currency", "")).upper(),
        }
        if actual != expected:
            raise IbkrCompletedBarSourceError("IBKR history contract differs from the verified PAPER mapping")

    def _to_bar(self, row: Mapping[str, Any], session_date: date) -> Bar:
        session = self._session(session_date)
        assert session is not None
        values: dict[str, Decimal] = {}
        for key in ("open", "high", "low", "close", "volume"):
            try:
                value = Decimal(str(row[key]))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise IbkrCompletedBarSourceError(f"IBKR daily bar {key} is invalid") from exc
            if not value.is_finite():
                raise IbkrCompletedBarSourceError(f"IBKR daily bar {key} is not finite")
            values[key] = value
        try:
            return Bar(
                instrument_id=self.contract.instrument_id,
                session_date=session_date,
                start_at=session.open_at,
                end_at=session.close_at,
                # Historical OHLC becomes usable no earlier than that
                # session's close. The adapter separately records that IBKR
                # historical publication/adjustment timing is unverified.
                # Stamping every row with retrieval time would give an entire
                # history window one duplicate timestamp and break replay.
                available_at=session.close_at,
                timezone="America/New_York",
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=values["volume"],
                currency=self.contract.currency,
                is_final=True,
                source="ibkr_paper_gateway_historical_trades",
                adjustment="provider_adjustment_unverified",
                quality_flags=("point_in_time_availability_unverified", "corporate_action_adjustment_unverified"),
            )
        except Exception as exc:
            raise IbkrCompletedBarSourceError("IBKR daily bar does not satisfy the frozen Bar contract") from exc

    @staticmethod
    def _session(session_date: date):
        try:
            schedule = build_xnys_schedule(session_date, session_date)
        except Exception as exc:
            raise IbkrCompletedBarSourceError("pinned XNYS calendar is unavailable") from exc
        return schedule.sessions[0] if schedule.sessions else None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bar source clock must return an aware datetime")
        return value.astimezone(timezone.utc)


__all__ = ["IbkrCompletedBarSourceError", "IbkrPaperCompletedBarSource"]
