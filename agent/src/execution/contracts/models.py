"""Core execution schemas frozen from docs/backtrader-system/CONTRACTS.md.

These models intentionally contain only stable domain contracts. Broker SDK
objects and service-specific persistence records belong in their adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceKind(StrEnum):
    NATURAL_LANGUAGE_TEMPLATE = "natural_language_template"
    PYTHON_UPLOAD = "python_upload"


class Environment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class RunState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    RECONCILING = "RECONCILING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class LocalSubmissionState(StrEnum):
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    RESERVED = "RESERVED"
    QUEUED_FOR_SESSION = "QUEUED_FOR_SESSION"
    SEND_STARTED = "SEND_STARTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class BrokerOrderState(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PositionMode(StrEnum):
    TARGET_FRACTION = "target_fraction"
    EXPLICIT_QUANTITY = "explicit_quantity"


class BarPolicy(StrictModel):
    timeframe: str
    session: str
    completed_only: bool
    calendar_id: str
    timezone: str

    @field_validator("timeframe")
    @classmethod
    def supported_timeframe(cls, value: str) -> str:
        if value != "1d":
            raise ValueError("V1 execution supports completed daily bars only")
        return value

    @field_validator("session")
    @classmethod
    def supported_session(cls, value: str) -> str:
        if value != "regular":
            raise ValueError("V1 execution supports regular sessions only")
        return value

    @field_validator("completed_only")
    @classmethod
    def require_completed_bars(cls, value: bool) -> bool:
        if not value:
            raise ValueError("strategy input must contain completed bars only")
        return value


class PositionPolicy(StrictModel):
    mode: PositionMode
    long_only: bool = True
    whole_shares: bool = True
    allocation_fraction: Annotated[Decimal | None, Field(gt=Decimal("0"), le=Decimal("1"))] = None
    rebalance: str | None = None
    initial_entry: str | None = None

    @model_validator(mode="after")
    def sizing_fields_match_mode(self) -> "PositionPolicy":
        if not self.whole_shares:
            raise ValueError("V1 execution uses whole shares")
        if self.mode is PositionMode.TARGET_FRACTION:
            if self.allocation_fraction is None:
                raise ValueError("target_fraction requires allocation_fraction")
        elif self.allocation_fraction is not None:
            raise ValueError("explicit_quantity cannot also declare allocation_fraction")
        if not self.long_only:
            raise ValueError("V1 execution is long-only")
        return self


class ExecutionPolicy(StrictModel):
    decision: str
    dispatch: str
    order_type: str
    time_in_force: str
    outside_rth: bool
    gap_action: str
    missed_session_action: str

    @model_validator(mode="after")
    def v1_policy(self) -> "ExecutionPolicy":
        if self.decision != "completed_bar_available":
            raise ValueError("V1 decisions occur when a completed bar becomes available")
        if self.dispatch != "next_regular_session":
            raise ValueError("V1 orders dispatch in the next regular session")
        if self.order_type != "market" or self.time_in_force != "DAY":
            raise ValueError("V1 supports Market/DAY orders only")
        if self.outside_rth:
            raise ValueError("V1 orders cannot execute outside regular hours")
        return self


class StrategySpec(StrictModel):
    schema_version: str
    strategy_id: str
    source_kind: SourceKind
    template_id: str | None = None
    template_version: str | None = None
    source_artifact_id: str | None = None
    instrument_ids: tuple[str, ...]
    bar_policy: BarPolicy
    parameters: dict[str, JsonValue]
    position_policy: PositionPolicy
    execution_policy: ExecutionPolicy

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("unsupported StrategySpec schema_version")
        return value

    @field_validator("instrument_ids")
    @classmethod
    def one_instrument(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != 1 or not value[0]:
            raise ValueError("V1 strategy must identify exactly one instrument")
        return value

    @model_validator(mode="after")
    def source_matches_payload(self) -> "StrategySpec":
        if self.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE:
            if not self.template_id or not self.template_version or self.source_artifact_id:
                raise ValueError("template source requires template id/version and no upload artifact")
        if self.source_kind is SourceKind.PYTHON_UPLOAD:
            if not self.source_artifact_id or self.template_id or self.template_version:
                raise ValueError("Python upload requires source_artifact_id and no template fields")
            if self.position_policy.mode is not PositionMode.EXPLICIT_QUANTITY:
                raise ValueError("V1 Python uploads use explicit_quantity unless compiled through the target helper")
        return self


class Instrument(StrictModel):
    instrument_id: str
    asset_class: str
    symbol: str
    currency: str
    venue: str
    calendar: str
    timezone: str
    lot_size: Annotated[int, Field(gt=0)]
    tick_size: Annotated[Decimal, Field(gt=Decimal("0"))]
    multiplier: Annotated[Decimal, Field(gt=Decimal("0"))]


class OrderIntent(StrictModel):
    intent_id: str
    decision_id: str
    dedupe_key: str
    account_ref: str
    environment: Environment
    side: str
    integer_qty: Annotated[int, Field(gt=0)]
    order_type: str
    time_in_force: str
    reference_price: Annotated[Decimal, Field(gt=Decimal("0"))]
    eligible_at: datetime
    expires_at: datetime
    risk_snapshot_id: str

    @field_validator("side")
    @classmethod
    def supported_side(cls, value: str) -> str:
        if value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return value

    @field_validator("eligible_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_window(self) -> "OrderIntent":
        if self.expires_at <= self.eligible_at:
            raise ValueError("expires_at must be after eligible_at")
        if self.order_type != "market" or self.time_in_force != "DAY":
            raise ValueError("V1 supports Market/DAY intents only")
        return self


class EventEnvelope(StrictModel):
    schema_version: str = "1.0"
    event_id: str
    seq: Annotated[int, Field(gt=0)]
    deployment_id: str
    run_id: str
    account_ref: str
    environment: Environment
    kind: str
    occurred_at: datetime
    received_at: datetime
    persisted_at: datetime
    entity_id: str | None = None
    payload: dict[str, JsonValue]

    @field_validator("occurred_at", "received_at", "persisted_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def timestamp_order(self) -> "EventEnvelope":
        if self.received_at < self.occurred_at:
            raise ValueError("received_at cannot precede occurred_at")
        if self.persisted_at < self.received_at:
            raise ValueError("persisted_at cannot precede received_at")
        return self


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CONTRACT_SCHEMA_VERSION = 1


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Remove representation-only trailing zeros while retaining exactness.
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical JSON does not accept naive datetimes")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python", exclude_none=False))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("canonical JSON does not accept non-finite floats")
        # Convert through JSON's deterministic shortest representation.
        return json.loads(json.dumps(value, allow_nan=False))
    return value


def canonical_json(value: BaseModel | dict[str, Any]) -> str:
    """Serialize contract data with stable keys, UTF-8 semantics and no NaN."""
    data = value.model_dump(mode="python", exclude_none=False) if isinstance(value, BaseModel) else value
    return json.dumps(_json_value(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_sha256(value: str) -> bool:
    """Small helper for artifact hash validators at service boundaries."""
    return bool(_HASH_RE.fullmatch(value))


class RiskGrant(StrictModel):
    """Explicit user-authorized limits bound to one immutable deployment."""

    schema_version: str = "1.0"
    grant_id: str
    user_action_id: str
    strategy_version_hash: str
    broker: str
    account_ref: str
    environment: Environment
    allowed_instruments: tuple[str, ...]
    allowed_order_types: tuple[str, ...]
    long_only: bool
    whole_shares: bool
    max_order_notional: Annotated[Decimal, Field(gt=Decimal("0"))]
    max_gross_exposure: Annotated[Decimal, Field(gt=Decimal("0"))]
    strategy_budget: Annotated[Decimal, Field(gt=Decimal("0"))]
    max_daily_loss: Annotated[Decimal, Field(gt=Decimal("0"))]
    max_orders_per_session: Annotated[int, Field(gt=0)]
    valid_from: datetime
    valid_until: datetime
    quote_max_age_seconds: Annotated[int, Field(gt=0)]
    account_max_age_seconds: Annotated[int, Field(gt=0)]
    halt_action: str
    liquidation_policy: str
    granted_at: datetime
    revoked_at: datetime | None = None

    @field_validator("strategy_version_hash")
    @classmethod
    def valid_strategy_hash(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("strategy_version_hash must be lowercase SHA-256")
        return value

    @field_validator("allowed_instruments")
    @classmethod
    def require_instruments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("allowed_instruments must be non-empty and unique")
        return value

    @field_validator("allowed_order_types")
    @classmethod
    def require_order_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(item != "market" for item in value):
            raise ValueError("V1 grants support Market orders only")
        if len(set(value)) != len(value):
            raise ValueError("allowed_order_types must be unique")
        return value

    @field_validator("long_only", "whole_shares")
    @classmethod
    def require_v1_position_rules(cls, value: bool) -> bool:
        if not value:
            raise ValueError("V1 grants require long-only, whole-share rules")
        return value

    @field_validator("valid_from", "valid_until", "granted_at", "revoked_at")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("grant timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_grant_window(self) -> "RiskGrant":
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if not (self.valid_from <= self.granted_at <= self.valid_until):
            raise ValueError("granted_at must fall within the grant validity window")
        if self.revoked_at is not None and self.revoked_at < self.granted_at:
            raise ValueError("revoked_at cannot precede granted_at")
        if self.halt_action != "pause_and_cancel_owned":
            raise ValueError("V1 halt action is pause_and_cancel_owned")
        if self.liquidation_policy != "none":
            raise ValueError("V1 does not authorize automatic liquidation")
        return self
