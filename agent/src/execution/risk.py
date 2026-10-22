"""Fail-closed, pure pre-dispatch risk checks for an explicitly granted run.

This module has no broker side effects and accepts only caller-supplied trusted
account/quote facts. It is an offline decision primitive, not an account feed,
serialized authorization service, or order dispatcher.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from src.execution.contracts import Environment, RiskGrant, canonical_sha256
from src.execution.contracts.models import StrictModel


class ProposedOrder(StrictModel):
    instrument_id: str
    side: str
    integer_qty: int = Field(strict=True, gt=0)
    order_type: str = "market"
    time_in_force: str = "DAY"

    @field_validator("side")
    @classmethod
    def valid_side(cls, value: str) -> str:
        if value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return value

    @field_validator("order_type", "time_in_force")
    @classmethod
    def market_day_only(cls, value: str, info) -> str:
        expected = "market" if info.field_name == "order_type" else "DAY"
        if value != expected:
            raise ValueError(f"V1 supports {expected} {info.field_name} only")
        return value


class PositionFact(StrictModel):
    instrument_id: str
    quantity: int = Field(strict=True)
    market_price: Decimal

    @field_validator("market_price")
    @classmethod
    def finite_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("market_price must be finite and positive")
        return value


class RiskFacts(StrictModel):
    """Fresh trusted facts already bound to one execution identity.

    ``available_cash`` is the confirmed cash before the separately supplied
    account buy reservations; positions must cover the account's USD holdings.
    Quotes, fees and a conservative price buffer must come from the trusted
    service's versioned cost/market rules, not the strategy worker.
    """

    broker: str
    account_ref: str
    environment: Environment
    strategy_version_hash: str
    instrument_id: str
    now: datetime
    quote_as_of: datetime
    account_as_of: datetime
    quote_price: Decimal
    price_buffer_fraction: Decimal = Decimal("0")
    estimated_fees: Decimal = Decimal("0")
    net_liquidation: Decimal
    available_cash: Decimal
    positions: tuple[PositionFact, ...]
    strategy_position_qty: int = Field(strict=True, ge=0)
    strategy_sell_reservations: int = Field(default=0, strict=True, ge=0)
    strategy_buy_reservations: Decimal
    account_buy_reservations: Decimal
    strategy_daily_loss: Decimal
    orders_this_session: int = Field(strict=True, ge=0)
    account_snapshot_complete: bool

    @field_validator("now", "quote_as_of", "account_as_of")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("risk timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator(
        "quote_price", "price_buffer_fraction", "estimated_fees", "net_liquidation", "available_cash",
        "strategy_buy_reservations", "account_buy_reservations", "strategy_daily_loss",
    )
    @classmethod
    def finite_amounts(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("risk monetary inputs must be finite")
        return value

    @field_validator("price_buffer_fraction", "estimated_fees")
    @classmethod
    def nonnegative_cost_inputs(cls, value: Decimal) -> Decimal:
        # A negative buffer or fee would reduce the risk estimate and can make
        # an otherwise over-budget buy appear affordable.
        if value < 0:
            raise ValueError("price buffer and estimated fees must be non-negative")
        return value


class RiskDecision(StrictModel):
    allowed: bool
    reason_codes: tuple[str, ...]
    checked_at: datetime
    risk_snapshot_id: str
    estimated_notional: Decimal
    estimated_cost: Decimal
    projected_strategy_exposure: Decimal
    projected_gross_exposure: Decimal

    @field_validator("checked_at")
    @classmethod
    def checked_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


def evaluate_order_risk(
    grant: RiskGrant | dict,
    order: ProposedOrder | dict,
    facts: RiskFacts | dict,
) -> RiskDecision:
    """Evaluate one exact-size request; rejects instead of silently shrinking.

    In addition to grant identity/time checks, buys reserve the whole candidate
    notional against cash, strategy budget, per-order and gross exposure limits.
    Sells cannot exceed strategy-owned shares. Incomplete or stale facts fail
    closed. The snapshot ID hashes every input to the decision.
    """
    grant = grant if isinstance(grant, RiskGrant) else RiskGrant.model_validate(grant)
    order = order if isinstance(order, ProposedOrder) else ProposedOrder.model_validate(order)
    facts = facts if isinstance(facts, RiskFacts) else RiskFacts.model_validate(facts)
    reasons: list[str] = []

    now = facts.now
    if grant.revoked_at is not None and grant.revoked_at <= now:
        reasons.append("grant_revoked")
    if now < grant.valid_from or now >= grant.valid_until:
        reasons.append("grant_expired_or_not_yet_valid")
    if facts.broker != grant.broker:
        reasons.append("broker_mismatch")
    if facts.account_ref != grant.account_ref:
        reasons.append("account_mismatch")
    if facts.environment is not grant.environment:
        reasons.append("environment_mismatch")
    if facts.strategy_version_hash != grant.strategy_version_hash:
        reasons.append("strategy_version_mismatch")
    if facts.instrument_id != order.instrument_id or order.instrument_id not in grant.allowed_instruments:
        reasons.append("instrument_not_authorized")
    if order.order_type not in grant.allowed_order_types:
        reasons.append("order_type_not_authorized")
    if not facts.account_snapshot_complete:
        reasons.append("account_snapshot_incomplete")

    age_quote = (now - facts.quote_as_of).total_seconds()
    age_account = (now - facts.account_as_of).total_seconds()
    if age_quote < 0 or age_quote > grant.quote_max_age_seconds:
        reasons.append("quote_stale_or_future")
    if age_account < 0 or age_account > grant.account_max_age_seconds:
        reasons.append("account_stale_or_future")
    if facts.quote_price <= 0:
        reasons.append("invalid_quote")
    if facts.net_liquidation <= 0 or facts.available_cash < 0:
        reasons.append("invalid_account_values")
    if facts.strategy_buy_reservations < 0 or facts.account_buy_reservations < 0:
        reasons.append("invalid_reservations")
    if facts.strategy_daily_loss < 0:
        reasons.append("invalid_daily_loss")
    if facts.strategy_daily_loss >= grant.max_daily_loss:
        reasons.append("daily_loss_limit")
    if facts.orders_this_session >= grant.max_orders_per_session:
        reasons.append("session_order_limit")

    candidate = Decimal(order.integer_qty) * facts.quote_price
    buffered_notional = candidate * (Decimal("1") + facts.price_buffer_fraction)
    candidate_cost = buffered_notional + facts.estimated_fees
    existing_gross = sum((abs(Decimal(pos.quantity) * pos.market_price) for pos in facts.positions), Decimal("0"))
    projected_gross = existing_gross + facts.account_buy_reservations
    projected_strategy = (
        Decimal(facts.strategy_position_qty) * facts.quote_price + facts.strategy_buy_reservations
    )

    if buffered_notional > grant.max_order_notional:
        reasons.append("max_order_notional")
    if order.side == "BUY":
        projected_strategy += buffered_notional
        projected_gross += buffered_notional
        if projected_strategy > grant.strategy_budget:
            reasons.append("strategy_budget")
        if candidate_cost + facts.account_buy_reservations > facts.available_cash:
            reasons.append("available_cash")
        if projected_gross > grant.max_gross_exposure:
            reasons.append("max_gross_exposure")
    else:
        available_strategy_shares = facts.strategy_position_qty - facts.strategy_sell_reservations
        if available_strategy_shares < order.integer_qty:
            reasons.append("sell_exceeds_strategy_position")
        projected_strategy = max(
            Decimal("0"), projected_strategy - Decimal(order.integer_qty) * facts.quote_price
        )
        projected_gross = max(Decimal("0"), projected_gross - candidate)

    snapshot_inputs = {
        "grant": grant,
        "order": order,
        "facts": facts,
        "reasons": sorted(set(reasons)),
        "estimated_notional": candidate,
        "buffered_notional": buffered_notional,
        "estimated_cost": candidate_cost,
        "projected_strategy_exposure": projected_strategy,
        "projected_gross_exposure": projected_gross,
    }
    return RiskDecision(
        allowed=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        checked_at=now,
        risk_snapshot_id=canonical_sha256(snapshot_inputs),
        estimated_notional=candidate,
        estimated_cost=candidate_cost,
        projected_strategy_exposure=projected_strategy,
        projected_gross_exposure=projected_gross,
    )
