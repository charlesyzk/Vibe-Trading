"""Typed capability contracts for market data and market-specific rules.

These are adapter boundaries only. They do not authenticate a provider, calendar,
instrument mapping, or fee schedule. An adapter must opt into every capability it
actually implements; inherited methods fail closed instead of returning empty data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import AsyncIterator, NoReturn

from pydantic import Field, field_validator, model_validator

from src.bt_runtime.market_data import MarketSession
from src.execution.contracts import Bar, DataManifest, Instrument, is_sha256
from src.execution.contracts.models import StrictModel


def calendar_schedule_sha256(sessions: tuple[MarketSession, ...] | list[MarketSession]) -> str:
    """Return the exact schedule digest used by the completed-bar importer."""
    payload = [
        {
            "session_date": item.session_date.isoformat(),
            "open_at": item.open_at.astimezone(timezone.utc).isoformat(),
            "close_at": item.close_at.astimezone(timezone.utc).isoformat(),
            "early_close": item.early_close,
        }
        for item in sessions
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class MarketCapability(StrEnum):
    HISTORY = "history"
    REALTIME = "realtime"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class CapabilityDeclaration(StrictModel):
    """Explicit support declaration; all capability claims default to false."""

    history: bool = False
    realtime: bool = False
    backtest: bool = False
    paper: bool = False
    live: bool = False

    def require(self, capability: MarketCapability) -> None:
        if not getattr(self, capability.value):
            raise UnsupportedMarketCapability(f"market adapter does not support {capability.value}")


class UnsupportedMarketCapability(RuntimeError):
    """Raised when an adapter operation is outside its declared capability."""


class ProviderQualityState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class MarketAdapter:
    """Fail-closed base for small market adapters."""

    capabilities: CapabilityDeclaration = CapabilityDeclaration()

    def __init__(self, capabilities: CapabilityDeclaration | None = None) -> None:
        if capabilities is not None:
            self.capabilities = capabilities

    def require(self, *capabilities: MarketCapability) -> None:
        for capability in capabilities:
            self.capabilities.require(capability)


class MarketDataSnapshot(StrictModel):
    bars: tuple[Bar, ...]
    manifest: DataManifest

    @model_validator(mode="after")
    def matches_manifest(self) -> "MarketDataSnapshot":
        if not self.bars:
            raise ValueError("market-data snapshot must contain bars")
        if any(bar.instrument_id != self.manifest.instrument_id for bar in self.bars):
            raise ValueError("snapshot bars must match the manifest instrument")
        dates = tuple(bar.session_date for bar in self.bars)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("snapshot bars must have unique increasing session dates")
        if dates[0] != self.manifest.range_start or dates[-1] != self.manifest.range_end:
            raise ValueError("snapshot bars must span the manifest data range")
        if self.manifest.warmup_end not in dates or self.manifest.evaluation_start not in dates:
            raise ValueError("snapshot bars must include the manifest warmup/evaluation boundary sessions")
        if any(bar.available_at > self.manifest.fetched_at for bar in self.bars):
            raise ValueError("snapshot fetched_at cannot precede a bar available_at")
        if self.manifest.schema_version == "1.1":
            if any(bar.currency != self.manifest.currency for bar in self.bars):
                raise ValueError("snapshot bar currency must match the manifest currency")
            if any(bar.source != self.manifest.provider for bar in self.bars):
                raise ValueError("snapshot bar source must match the manifest provider")
            if any(bar.adjustment != self.manifest.adjustment_policy for bar in self.bars):
                raise ValueError("snapshot bar adjustment must match the manifest policy")
            if any(not bar.is_final for bar in self.bars):
                raise ValueError("market-data snapshots require completed bars")
        return self


class ProviderQuality(StrictModel):
    provider: str
    status: ProviderQualityState
    observed_at: datetime
    details: dict[str, str] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def nonempty_provider(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider must be non-empty")
        return value

    @field_validator("observed_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(timezone.utc)


class MarketDataProvider(MarketAdapter):
    """Provider contract for contract confirmation, history, bars, and health.

    Realtime streams in this first contract carry completed ``Bar`` records.
    Quote streaming is rejected until a versioned quote contract is added.
    """

    def confirm_instrument(self, instrument_id: str) -> Instrument:
        self.require(MarketCapability.HISTORY)
        raise UnsupportedMarketCapability("instrument confirmation is not implemented")

    def history_snapshot(
        self, instrument_id: str, start: date, end: date, *, mode: MarketCapability
    ) -> MarketDataSnapshot:
        self.require(MarketCapability.HISTORY, mode)
        raise UnsupportedMarketCapability("historical snapshot is not implemented")

    def subscribe_completed_bars(
        self, instrument_id: str, *, mode: MarketCapability
    ) -> AsyncIterator[Bar]:
        self.require(MarketCapability.REALTIME, mode)
        raise UnsupportedMarketCapability("completed-bar subscription is not implemented")

    def subscribe_quotes(self, instrument_id: str, *, mode: MarketCapability) -> NoReturn:
        self.require(MarketCapability.REALTIME, mode)
        raise UnsupportedMarketCapability("quote contract is not defined; quote subscription is unavailable")

    def quality_status(self) -> ProviderQuality:
        raise UnsupportedMarketCapability("provider quality status is not implemented")


class CalendarSchedule(StrictModel):
    calendar_id: str
    version: str
    start_date: date
    end_date: date
    sessions: tuple[MarketSession, ...]
    sha256: str

    @field_validator("calendar_id", "version")
    @classmethod
    def nonempty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("calendar_id and version must be non-empty")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_schedule_hash(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("calendar schedule sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def valid_schedule(self) -> "CalendarSchedule":
        if self.end_date < self.start_date:
            raise ValueError("calendar schedule end_date must not precede start_date")
        dates = tuple(session.session_date for session in self.sessions)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("calendar sessions must have unique increasing dates")
        if any(not self.start_date <= day <= self.end_date for day in dates):
            raise ValueError("calendar sessions must be within the requested date range")
        if self.sha256 != calendar_schedule_sha256(self.sessions):
            raise ValueError("calendar schedule sha256 does not match the supplied sessions")
        return self


class TradingCalendar(MarketAdapter):
    """Versioned market-session source. Sessions use the existing MarketSession type."""

    def schedule(self, start: date, end: date, *, mode: MarketCapability) -> CalendarSchedule:
        self.require(mode)
        raise UnsupportedMarketCapability("calendar schedule is not implemented")

    def session(self, session_date: date, *, mode: MarketCapability) -> MarketSession | None:
        self.require(mode)
        raise UnsupportedMarketCapability("calendar session lookup is not implemented")

    def next_session(self, after: date, *, mode: MarketCapability) -> MarketSession:
        self.require(mode)
        raise UnsupportedMarketCapability("next calendar session lookup is not implemented")


class MarketRulesSnapshot(StrictModel):
    instrument: Instrument
    supports_sell: bool
    supported_order_types: tuple[str, ...]
    regular_session_only: bool = True
    rules_version: str

    @model_validator(mode="after")
    def valid_rules(self) -> "MarketRulesSnapshot":
        if not self.supported_order_types or len(set(self.supported_order_types)) != len(self.supported_order_types):
            raise ValueError("supported_order_types must be non-empty and unique")
        if not self.rules_version.strip():
            raise ValueError("rules_version must be non-empty")
        return self


class MarketRules(MarketAdapter):
    """Precision, lot, sell, order-type, and session rules for one instrument."""

    def rules(self, instrument_id: str, *, mode: MarketCapability) -> MarketRulesSnapshot:
        self.require(mode)
        raise UnsupportedMarketCapability("market rules are not implemented")


class CostInput(StrictModel):
    instrument_id: str
    side: str
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=Decimal("0"))
    currency: str

    @field_validator("instrument_id", "currency")
    @classmethod
    def nonempty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instrument_id and currency must be non-empty")
        return value

    @field_validator("quantity", mode="before")
    @classmethod
    def positive_whole_quantity(cls, value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("quantity must be a positive whole integer")
        return value

    @field_validator("side")
    @classmethod
    def supported_side(cls, value: str) -> str:
        if value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return value


class CostEstimate(StrictModel):
    currency: str
    total: Decimal = Field(ge=Decimal("0"))
    components: dict[str, Decimal]
    model_version: str

    @model_validator(mode="after")
    def components_sum(self) -> "CostEstimate":
        if not self.model_version.strip():
            raise ValueError("model_version must be non-empty")
        if any(value < 0 for value in self.components.values()):
            raise ValueError("cost components cannot be negative")
        if sum(self.components.values(), Decimal("0")) != self.total:
            raise ValueError("cost components must sum exactly to total")
        return self


class CostModel(MarketAdapter):
    """Versioned fee/slippage estimate boundary; no fee schedule is assumed."""

    def estimate(self, order: CostInput, *, mode: MarketCapability) -> CostEstimate:
        self.require(mode)
        raise UnsupportedMarketCapability("cost estimate is not implemented")
