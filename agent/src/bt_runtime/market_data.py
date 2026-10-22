"""Strict import boundary for provider-supplied completed US equity bars.

The importer verifies bytes, bar quality, timestamps, and completeness against
an explicitly versioned market-session schedule. It does not download data or
claim that caller-supplied provider/license metadata has been authenticated.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import JsonValue, field_validator

from src.bt_runtime.strategies.sma import _validate_bars
from src.execution.contracts import Bar, DataManifest
from src.execution.contracts.models import StrictModel


MAX_MARKET_DATA_BYTES = 64 * 1024 * 1024
MAX_MARKET_DATA_ROWS = 100_000
CSV_COLUMNS = (
    "session_date",
    "start_at",
    "end_at",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class MarketDataMetadata(StrictModel):
    provider: str
    instrument_id: str
    currency: str
    timezone: str
    calendar_id: str
    calendar_version: str
    calendar_schedule_sha256: str | None = None
    availability_model: str
    adjustment_policy: str
    corporate_action_policy: str
    fetched_at: datetime
    warmup_start: date
    warmup_end: date
    evaluation_start: date
    evaluation_end: date
    download_parameters: dict[str, JsonValue]

    @field_validator(
        "provider", "instrument_id", "currency", "timezone", "calendar_id", "calendar_version",
        "availability_model", "adjustment_policy", "corporate_action_policy",
    )
    @classmethod
    def require_nonempty_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("market-data metadata labels must be non-empty")
        return value


@dataclass(frozen=True, slots=True)
class MarketSession:
    """One authoritative scheduled regular session supplied by a calendar."""

    session_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool = False

    def __post_init__(self) -> None:
        for name in ("open_at", "close_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone")
        if self.close_at <= self.open_at:
            raise ValueError("session close must be after open")


@dataclass(frozen=True, slots=True)
class ImportedMarketData:
    bars: pd.DataFrame
    bar_records: tuple[Bar, ...]
    manifest: DataManifest
    raw_sha256: str


def import_completed_daily_csv(
    raw_csv: bytes,
    metadata: MarketDataMetadata | dict[str, Any],
    expected_sessions: Sequence[MarketSession],
) -> ImportedMarketData:
    """Import a byte-exact OHLCV file against an explicit session schedule.

    The complete expected schedule must exactly equal the file's session dates
    for the requested data interval. Bars are indexed by their availability
    timestamp so Backtrader decisions cannot occur before each completed bar
    becomes available. The calendar producer and provider source are named in
    the manifest, while their external authenticity remains unverified here.
    """
    if not isinstance(metadata, MarketDataMetadata):
        metadata = MarketDataMetadata.model_validate(metadata)
    if not isinstance(raw_csv, bytes) or not raw_csv:
        raise ValueError("market-data CSV must be non-empty bytes")
    if len(raw_csv) > MAX_MARKET_DATA_BYTES:
        raise ValueError("market-data CSV exceeds the configured byte limit")
    if not expected_sessions:
        raise ValueError("an explicit, versioned market-session schedule is required")
    if not metadata.provider.strip() or metadata.provider == "synthetic_fixture":
        raise ValueError("real market data requires a non-synthetic provider label")

    try:
        source = pd.read_csv(io.BytesIO(raw_csv), dtype=str, keep_default_na=False)
    except (UnicodeError, pd.errors.ParserError, ValueError) as exc:
        raise ValueError("market-data CSV could not be parsed") from exc
    if tuple(source.columns) != CSV_COLUMNS:
        raise ValueError(f"market-data CSV columns must be exactly {CSV_COLUMNS}")
    if source.empty:
        raise ValueError("market-data CSV must contain at least one bar")
    if len(source) > MAX_MARKET_DATA_ROWS:
        raise ValueError("market-data CSV has too many rows")

    records: list[Bar] = []
    for row_number, row in enumerate(source.to_dict(orient="records"), start=2):
        try:
            session_day = date.fromisoformat(row["session_date"])
            timestamps = {
                name: _aware_timestamp(row[name])
                for name in ("start_at", "end_at", "available_at")
            }
            values = {
                name: Decimal(row[name])
                for name in ("open", "high", "low", "close", "volume")
            }
            records.append(
                Bar(
                    instrument_id=metadata.instrument_id,
                    session_date=session_day,
                    start_at=timestamps["start_at"],
                    end_at=timestamps["end_at"],
                    available_at=timestamps["available_at"],
                    timezone=metadata.timezone,
                    open=values["open"],
                    high=values["high"],
                    low=values["low"],
                    close=values["close"],
                    volume=values["volume"],
                    currency=metadata.currency,
                    is_final=True,
                    source=metadata.provider,
                    adjustment=metadata.adjustment_policy,
                )
            )
        except (ValueError, InvalidOperation, KeyError) as exc:
            raise ValueError(f"invalid completed bar at CSV row {row_number}: {exc}") from exc

    fetched_at = _aware_timestamp(metadata.fetched_at)
    latest_available_at = max(record.available_at for record in records)
    if fetched_at < latest_available_at:
        raise ValueError("market-data fetched_at cannot precede the latest bar available_at")

    session_dates = [record.session_date for record in records]
    if session_dates != sorted(set(session_dates)):
        raise ValueError("session_date rows must be unique and strictly increasing")
    sessions = sorted(expected_sessions, key=lambda item: item.session_date)
    expected_dates = [session.session_date for session in sessions]
    if expected_dates != sorted(set(expected_dates)):
        raise ValueError("expected market sessions must be unique and strictly increasing")
    schedule_payload = [
        {
            "session_date": session.session_date.isoformat(),
            "open_at": _aware_timestamp(session.open_at).isoformat(),
            "close_at": _aware_timestamp(session.close_at).isoformat(),
            "early_close": session.early_close,
        }
        for session in sessions
    ]
    schedule_sha256 = hashlib.sha256(
        json.dumps(schedule_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if metadata.calendar_schedule_sha256 and metadata.calendar_schedule_sha256 != schedule_sha256:
        raise ValueError("calendar schedule hash does not match the supplied sessions")
    if session_dates != expected_dates:
        missing = sorted(set(expected_dates) - set(session_dates))
        unexpected = sorted(set(session_dates) - set(expected_dates))
        raise ValueError(
            f"market session coverage mismatch; missing={missing[:5]}, unexpected={unexpected[:5]}"
        )
    if metadata.warmup_start != session_dates[0] or metadata.evaluation_end != session_dates[-1]:
        raise ValueError("manifest warmup/evaluation range must span the imported market sessions")
    if metadata.warmup_end not in session_dates:
        raise ValueError("manifest warmup_end must be an imported market session")
    if metadata.evaluation_start not in session_dates:
        raise ValueError("manifest evaluation_start must be an imported market session")

    session_timezone = ZoneInfo(metadata.timezone)
    for record, session in zip(records, sessions, strict=True):
        expected_open = _aware_timestamp(session.open_at)
        expected_close = _aware_timestamp(session.close_at)
        if (
            expected_open.astimezone(session_timezone).date() != session.session_date
            or expected_close.astimezone(session_timezone).date() != session.session_date
        ):
            raise ValueError(
                f"calendar session timestamps do not fall on {session.session_date} "
                f"in declared timezone {metadata.timezone}"
            )
        if record.start_at != expected_open or record.end_at != expected_close:
            raise ValueError(f"bar timestamps do not match the calendar for {record.session_date}")
        if record.available_at < record.end_at:
            raise ValueError(f"bar was available before session close for {record.session_date}")

    # Do not allow Decimal infinities to become an unchecked float in pandas.
    if any(not getattr(record, field).is_finite() for record in records for field in ("open", "high", "low", "close", "volume")):
        raise ValueError("OHLCV values must be finite")
    bars = pd.DataFrame(
        {
            "open": [float(record.open) for record in records],
            "high": [float(record.high) for record in records],
            "low": [float(record.low) for record in records],
            "close": [float(record.close) for record in records],
            "volume": [float(record.volume) for record in records],
        },
        index=pd.DatetimeIndex([record.available_at for record in records], name="available_at"),
    )
    bars = _validate_bars(bars)
    strategy_input_sha256 = canonical_daily_bars_sha256(bars)

    digest = hashlib.sha256(raw_csv).hexdigest()
    dates = session_dates
    manifest = DataManifest(
        manifest_id=f"{metadata.provider}:{metadata.instrument_id}:{digest[:16]}",
        provider=metadata.provider,
        instrument_id=metadata.instrument_id,
        currency=metadata.currency,
        fields=("session_date", "start_at", "end_at", "available_at", "open", "high", "low", "close", "volume"),
        range_start=dates[0],
        range_end=dates[-1],
        availability_model=metadata.availability_model,
        calendar_version=f"{metadata.calendar_id}@{metadata.calendar_version}",
        adjustment_policy=metadata.adjustment_policy,
        corporate_action_policy=metadata.corporate_action_policy,
        download_parameters=metadata.download_parameters,
        fetched_at=fetched_at,
        file_sha256=digest,
        quality_report={
            "validation": "passed",
            "session_schedule": "exact_match",
            "calendar_provider": metadata.calendar_id,
            "calendar_version": metadata.calendar_version,
            "calendar_schedule_sha256": schedule_sha256,
            "strategy_input_schema": "vibe-daily-ohlcv/1",
            "strategy_input_sha256": strategy_input_sha256,
            "calendar_verification": (
                "schedule_hash_matched" if metadata.calendar_schedule_sha256 else "caller_supplied_schedule"
            ),
            "early_close_sessions": [
                session.session_date.isoformat() for session in sessions if session.early_close
            ],
            "provider_attestation": "caller_declared_unverified",
            "adjustment_attestation": "caller_declared_unverified",
        },
        warmup_start=metadata.warmup_start,
        warmup_end=metadata.warmup_end,
        evaluation_start=metadata.evaluation_start,
        evaluation_end=metadata.evaluation_end,
        synthetic=False,
    )
    return ImportedMarketData(bars=bars, bar_records=tuple(records), manifest=manifest, raw_sha256=digest)


def canonical_daily_bars_sha256(bars: pd.DataFrame) -> str:
    """Hash the exact normalized OHLCV values and timestamps consumed by BT.

    ``file_sha256`` identifies the original CSV bytes. This separate digest
    binds the imported strategy input after parsing and normalization, so a
    caller cannot reuse a valid manifest with a same-range, altered frame.
    """
    frame = _validate_bars(bars)
    payload = [
        {
            "available_at": timestamp.isoformat(),
            **{field: format(float(row[field]), ".17g") for field in CSV_COLUMNS[-5:]},
        }
        for timestamp, row in frame.iterrows()
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_timestamp(value: str | datetime) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)
