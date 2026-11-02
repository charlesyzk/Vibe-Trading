"""Combine host strategy metrics with durable PAPER submission counts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.execution.ibkr_paper_risk_facts import IbkrPaperRiskFactsError, StrategyRiskMetrics


class LedgerStrategyMetricsReader:
    """Add scoped, durable New York session submission counts to host facts.

    The wrapped source must still supply strategy position, P&L and costs.
    This counts attempts including rejected and unknown sends, not broker fills.
    """

    def __init__(self, *, database: str | Path, account_ref: str, source: Callable,
                 previous_bar_reader: Callable | None = None) -> None:
        self.database = Path(database)
        self.account_ref = account_ref
        self.source = source
        self.previous_bar_reader = previous_bar_reader
        if not callable(source):
            raise TypeError("strategy metrics source must be callable")

    def __call__(self, version_id: str, instrument_id: str, now: datetime) -> StrategyRiskMetrics:
        if now.tzinfo is None or now.utcoffset() is None:
            raise IbkrPaperRiskFactsError("strategy metrics clock must be timezone-aware")
        instant = now.astimezone(timezone.utc)
        local_day = instant.astimezone(ZoneInfo("America/New_York")).date()
        start = datetime.combine(local_day, time.min, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        try:
            with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                rows = connection.execute(
                    "SELECT oi.body_json, sa.started_at FROM submission_attempt sa "
                    "JOIN order_intent oi ON oi.intent_id=sa.intent_id"
                ).fetchall()
            count = 0
            for body, stamp in rows:
                intent = json.loads(body)
                if (intent.get("broker") != "IBKR" or intent.get("account_ref") != self.account_ref
                        or intent.get("environment") != "paper" or intent.get("strategy_version_hash") != version_id):
                    continue
                timestamp = datetime.fromisoformat(stamp)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("submission timestamp has no timezone")
                timestamp = timestamp.astimezone(timezone.utc)
                if timestamp > instant:
                    raise ValueError("submission timestamp is in the future")
                if start <= timestamp < end:
                    count += 1
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise IbkrPaperRiskFactsError("durable session submission count is unavailable") from exc
        metrics = self.source(version_id, instrument_id, now)
        if not isinstance(metrics, StrategyRiskMetrics):
            raise IbkrPaperRiskFactsError("host did not provide complete strategy risk/cost facts")
        return replace(metrics, orders_this_session=max(metrics.orders_this_session, count))

    def daily_loss_at(self, version_id: str, instrument_id: str, account_as_of: datetime, mark: Decimal) -> Decimal:
        """Calculate day P&L from scoped fills, reported fees and an opening mark.

        Overnight positions require the previous session's trusted completed
        bar. Missing or revised fees and incomplete marks are not guessed.
        """
        from src.bt_runtime.calendars import build_xnys_schedule

        if account_as_of.tzinfo is None or account_as_of.utcoffset() is None:
            raise IbkrPaperRiskFactsError("account snapshot time must be timezone-aware")
        instant = account_as_of.astimezone(timezone.utc)
        day = instant.astimezone(ZoneInfo("America/New_York")).date()
        start = datetime.combine(day, time.min, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        mark = Decimal(str(mark))
        if not mark.is_finite() or mark <= 0:
            raise IbkrPaperRiskFactsError("a current positive strategy mark is required")
        try:
            with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                rows = connection.execute(
                    "SELECT oi.body_json,f.quantity,f.price,f.occurred_at,f.broker_exec_id "
                    "FROM fill f JOIN order_intent oi ON oi.intent_id=f.intent_id "
                    "WHERE f.broker='IBKR' AND f.account_ref=? AND f.environment='paper'",
                    (self.account_ref,),
                ).fetchall()
                fees = connection.execute(
                    "SELECT broker_exec_id,amount,currency FROM fee_fact "
                    "WHERE broker='IBKR' AND account_ref=? AND environment='paper'",
                    (self.account_ref,),
                ).fetchall()
            by_exec: dict[str, list[tuple[str, str]]] = {}
            for exec_id, amount, currency in fees:
                by_exec.setdefault(exec_id, []).append((amount, currency))
            opening = 0
            ending = 0
            cashflow = Decimal("0")
            for body, quantity, price, stamp, exec_id in rows:
                intent = json.loads(body)
                if intent.get("strategy_version_hash") != version_id or intent.get("instrument_id") != instrument_id:
                    continue
                timestamp = datetime.fromisoformat(stamp)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.astimezone(timezone.utc) >= instant:
                    raise ValueError("snapshot does not follow fill")
                timestamp = timestamp.astimezone(timezone.utc)
                if type(quantity) is not int or quantity <= 0 or intent.get("side") not in {"BUY", "SELL"}:
                    raise ValueError("invalid fill")
                price = Decimal(price)
                if not price.is_finite() or price <= 0:
                    raise ValueError("invalid fill price")
                signed = quantity if intent["side"] == "BUY" else -quantity
                ending += signed
                if timestamp < start:
                    opening += signed
                else:
                    cashflow -= Decimal(signed) * price
                    observations = by_exec.get(exec_id, [])
                    if len(observations) != 1 or observations[0][1] != "USD":
                        raise ValueError("fill fee is missing, revised or not in USD")
                    fee = Decimal(observations[0][0])
                    if not fee.is_finite() or fee < 0:
                        raise ValueError("fill fee is invalid")
                    cashflow -= fee
            if opening < 0 or ending < 0:
                raise ValueError("fills imply a short position")
            opening_value = Decimal("0")
            if opening:
                if not callable(self.previous_bar_reader):
                    raise ValueError("previous session close is unavailable")
                sessions = build_xnys_schedule(day - timedelta(days=14), day - timedelta(days=1)).sessions
                if not sessions:
                    raise ValueError("previous session is unavailable")
                bar = self.previous_bar_reader(instrument_id, sessions[-1].session_date)
                if bar.session_date != sessions[-1].session_date or bar.instrument_id != instrument_id or not bar.is_final:
                    raise ValueError("previous session bar does not match")
                close = Decimal(str(bar.close))
                if not close.is_finite() or close <= 0:
                    raise ValueError("previous session close is invalid")
                opening_value = Decimal(opening) * close
            pnl = Decimal(ending) * mark + cashflow - opening_value
            return max(Decimal("0"), -pnl)
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise IbkrPaperRiskFactsError("strategy daily loss facts are unavailable") from exc

    def position_at(self, version_id: str, instrument_id: str, account_as_of: datetime) -> int:
        """Derive strategy-owned shares from fills older than the account snapshot."""
        if account_as_of.tzinfo is None or account_as_of.utcoffset() is None:
            raise IbkrPaperRiskFactsError("account snapshot time must be timezone-aware")
        watermark = account_as_of.astimezone(timezone.utc)
        try:
            with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                rows = connection.execute(
                    "SELECT oi.body_json, f.quantity, f.occurred_at FROM fill f "
                    "JOIN order_intent oi ON oi.intent_id=f.intent_id "
                    "WHERE f.broker='IBKR' AND f.account_ref=? AND f.environment='paper'",
                    (self.account_ref,),
                ).fetchall()
            quantity = 0
            for body, filled, stamp in rows:
                intent = json.loads(body)
                if intent.get("strategy_version_hash") != version_id or intent.get("instrument_id") != instrument_id:
                    continue
                if (intent.get("broker") != "IBKR" or intent.get("account_ref") != self.account_ref
                        or intent.get("environment") != "paper" or intent.get("side") not in {"BUY", "SELL"}
                        or type(filled) is not int or filled <= 0):
                    raise ValueError("invalid scoped fill")
                timestamp = datetime.fromisoformat(stamp)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.astimezone(timezone.utc) >= watermark:
                    raise ValueError("account snapshot does not follow strategy fill")
                quantity += filled if intent["side"] == "BUY" else -filled
            if quantity < 0:
                raise ValueError("strategy fills imply a short position")
            return quantity
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise IbkrPaperRiskFactsError("strategy position cannot be established from reconciled fills") from exc


class LedgerComputedStrategyMetricsReader(LedgerStrategyMetricsReader):
    """Build complete strategy facts from the ledger and a trusted prior bar.

    The local ledger's reservations are deliberately zero in this external
    view: the dispatch transaction adds them separately to avoid double count.
    """

    def __init__(self, *, database: str | Path, account_ref: str, previous_bar_reader: Callable,
                 price_buffer_fraction: Decimal, estimated_fees: Decimal) -> None:
        if not callable(previous_bar_reader):
            raise ValueError("a trusted previous-session bar reader is required")
        buffer = Decimal(str(price_buffer_fraction))
        fees = Decimal(str(estimated_fees))
        if not buffer.is_finite() or not Decimal("0") <= buffer <= Decimal("1"):
            raise ValueError("price buffer must be between zero and one")
        if not fees.is_finite() or fees < 0:
            raise ValueError("estimated fees must be finite and non-negative")
        self._buffer = buffer
        self._fees = fees
        super().__init__(database=database, account_ref=account_ref, previous_bar_reader=previous_bar_reader,
                         source=lambda *_: StrategyRiskMetrics(0, 0, Decimal("0"), Decimal("0"), 0, buffer, fees))

    def __call__(self, *_args) -> StrategyRiskMetrics:
        raise IbkrPaperRiskFactsError("strategy metrics require a reconciled snapshot and current mark")

    def for_snapshot(self, version_id: str, instrument_id: str, now: datetime,
                     account_as_of: datetime, mark: Decimal) -> StrategyRiskMetrics:
        counts = super().__call__(version_id, instrument_id, now)
        return replace(counts, strategy_position_qty=self.position_at(version_id, instrument_id, account_as_of),
                       strategy_daily_loss=self.daily_loss_at(version_id, instrument_id, account_as_of, mark))
