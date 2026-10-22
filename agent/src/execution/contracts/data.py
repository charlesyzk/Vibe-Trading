"""Immutable market-data evidence contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, field_validator, model_validator

from .models import StrictModel, is_sha256


class Bar(StrictModel):
    instrument_id: str
    session_date: date
    start_at: datetime
    end_at: datetime
    available_at: datetime
    timezone: str
    open: Annotated[Decimal, Field(gt=Decimal("0"))]
    high: Annotated[Decimal, Field(gt=Decimal("0"))]
    low: Annotated[Decimal, Field(gt=Decimal("0"))]
    close: Annotated[Decimal, Field(gt=Decimal("0"))]
    volume: Annotated[Decimal, Field(ge=Decimal("0"))]
    currency: str
    is_final: bool
    source: str
    adjustment: str
    quality_flags: tuple[str, ...] = ()

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a known IANA timezone") from exc
        return value

    @field_validator("start_at", "end_at", "available_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bar timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_bar(self) -> "Bar":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.available_at < self.end_at:
            raise ValueError("available_at cannot precede the bar end")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be at most open, high, and close")
        return self


class DataManifest(StrictModel):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    manifest_id: str
    provider: str
    instrument_id: str
    # Added in v1.1. Keep the optional shape so stored v1.0 reports remain readable.
    currency: str | None = None
    broker_contract_map_id: str | None = None
    fields: tuple[str, ...]
    range_start: date
    range_end: date
    availability_model: str
    calendar_version: str
    adjustment_policy: str
    corporate_action_policy: str
    download_parameters: dict[str, JsonValue]
    fetched_at: datetime
    file_sha256: str
    quality_report: dict[str, JsonValue]
    warmup_start: date
    warmup_end: date
    evaluation_start: date
    evaluation_end: date
    synthetic: bool = False
    synthetic_note: str | None = None

    @field_validator("fetched_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("file_sha256")
    @classmethod
    def valid_file_hash(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("file_sha256 must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def valid_ranges_and_synthetic_label(self) -> "DataManifest":
        if self.schema_version == "1.1" and self.currency is None:
            raise ValueError("v1.1 data manifests require currency")
        if self.currency is not None and (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError("currency must be a three-letter uppercase code")
        if self.range_end < self.range_start:
            raise ValueError("range_end must not precede range_start")
        if not (self.range_start <= self.warmup_start <= self.warmup_end <= self.range_end):
            raise ValueError("warmup range must be ordered and contained in data range")
        if not (self.range_start <= self.evaluation_start <= self.evaluation_end <= self.range_end):
            raise ValueError("evaluation range must be ordered and contained in data range")
        if self.warmup_end >= self.evaluation_start:
            raise ValueError("warmup range must end before the evaluation range")
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("fields must be non-empty and unique")
        if self.synthetic and not self.synthetic_note:
            raise ValueError("synthetic manifests require an explicit synthetic_note")
        if not self.synthetic and self.synthetic_note is not None:
            raise ValueError("synthetic_note is only valid for synthetic data")
        return self
