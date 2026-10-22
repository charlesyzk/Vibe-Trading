"""Pure V1 position sizing helpers.

All money and price inputs are ``Decimal`` values. ``cash_available`` is the
confirmed USD cash already net of reservations. Fractions and buffers are
unitless ratios (for example, ``Decimal("0.02")`` means two percent).
Target and affordability quantities use floor rounding to whole shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from enum import StrEnum


class SizingReason(StrEnum):
    ACCEPTED = "accepted"
    CASH_SHRUNK = "cash_shrunk"
    NO_ORDER_NEEDED = "no_order_needed"
    ZERO_TARGET_SHARES = "zero_target_shares"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_SELLABLE_SHARES = "insufficient_sellable_shares"
    MISSING_INPUT = "missing_input"
    INVALID_INPUT = "invalid_input"
    STALE_INPUT = "stale_input"
    CASH_LIMIT = "cash_limit"
    BUDGET_LIMIT = "budget_limit"
    ORDER_LIMIT = "order_limit"


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Sizing outcome; ``accepted_qty=None`` means no order may be created.

    ``target_shares`` is populated for target-fraction buys. ``requested_qty``
    records the order quantity before a cash-based shrink, when calculable.
    """

    requested_qty: int | None
    accepted_qty: int | None
    reason: SizingReason
    target_shares: int | None = None

    @property
    def accepted(self) -> bool:
        return self.accepted_qty is not None and self.accepted_qty > 0


def _floor_shares(amount: Decimal, per_share: Decimal) -> int:
    return int((amount / per_share).to_integral_value(rounding=ROUND_FLOOR))


def _missing(*values: object) -> bool:
    return any(value is None for value in values)


def _is_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def size_target_fraction(
    *,
    entering_target: bool,
    allocation_fraction: Decimal | None,
    account_equity: Decimal | None,
    strategy_budget: Decimal | None,
    cash_available: Decimal | None,
    reference_price: Decimal | None,
    fee_reserve: Decimal | None,
    price_buffer_fraction: Decimal | None,
    held_qty: int | None,
    sellable_qty: int | None = None,
    account_fresh: bool = True,
    price_fresh: bool = True,
    position_fresh: bool = True,
) -> SizingResult:
    """Calculate a target-fraction entry delta or a full strategy exit.

    Entry target notional is ``min(allocation_fraction * E, B)``. Target
    shares are floor-rounded using ``P * (1 + S)``. The buy delta is
    ``max(0, target_shares - H)``. If that delta exceeds confirmed available
    cash after the fixed fee reserve, it is reduced at decision time to the
    greatest affordable whole-share quantity. The original delta remains in
    ``requested_qty`` and the result identifies the shrink.

    Exit requests all strategy-owned shares (H) and rejects the entire exit
    when fewer shares are confirmed sellable. It never invents or enlarges a
    sell quantity. No user authorization limits are inferred; callers pass
    configured values as inputs.
    """
    if _missing(reference_price, held_qty, price_buffer_fraction):
        return SizingResult(None, None, SizingReason.MISSING_INPUT)
    if not price_fresh or not position_fresh:
        return SizingResult(None, None, SizingReason.STALE_INPUT)
    if not _is_decimal(reference_price) or not _is_decimal(price_buffer_fraction):
        return SizingResult(None, None, SizingReason.INVALID_INPUT)
    if reference_price <= 0 or price_buffer_fraction < 0:
        return SizingResult(None, None, SizingReason.INVALID_INPUT)
    if isinstance(held_qty, bool) or not isinstance(held_qty, int) or held_qty < 0:
        return SizingResult(None, None, SizingReason.INVALID_INPUT)

    if not entering_target:
        if sellable_qty is None:
            return SizingResult(None, None, SizingReason.MISSING_INPUT)
        if not position_fresh:
            return SizingResult(None, None, SizingReason.STALE_INPUT)
        if isinstance(sellable_qty, bool) or not isinstance(sellable_qty, int) or sellable_qty < 0:
            return SizingResult(None, None, SizingReason.INVALID_INPUT)
        if held_qty == 0:
            return SizingResult(0, None, SizingReason.NO_ORDER_NEEDED, target_shares=0)
        if sellable_qty < held_qty:
            return SizingResult(held_qty, None, SizingReason.INSUFFICIENT_SELLABLE_SHARES, target_shares=0)
        return SizingResult(held_qty, held_qty, SizingReason.ACCEPTED, target_shares=0)

    required = (
        allocation_fraction,
        account_equity,
        strategy_budget,
        cash_available,
        fee_reserve,
    )
    if _missing(*required):
        return SizingResult(None, None, SizingReason.MISSING_INPUT)
    if not account_fresh:
        return SizingResult(None, None, SizingReason.STALE_INPUT)
    if not all(_is_decimal(value) for value in required):
        return SizingResult(None, None, SizingReason.INVALID_INPUT)

    # Type narrowing after the finite Decimal validation above.
    fraction = allocation_fraction
    equity = account_equity
    budget = strategy_budget
    cash = cash_available
    fee = fee_reserve
    assert fraction is not None and equity is not None and budget is not None
    assert cash is not None and fee is not None
    if not (Decimal("0") < fraction <= Decimal("1")):
        return SizingResult(None, None, SizingReason.INVALID_INPUT)
    if equity < 0 or budget < 0 or cash < 0 or fee < 0:
        return SizingResult(None, None, SizingReason.INVALID_INPUT)

    buffered_price = reference_price * (Decimal("1") + price_buffer_fraction)
    target_notional = min(fraction * equity, budget)
    target_shares = _floor_shares(target_notional, buffered_price)
    requested_qty = max(0, target_shares - held_qty)
    if target_shares == 0:
        return SizingResult(requested_qty, None, SizingReason.ZERO_TARGET_SHARES, target_shares)
    if requested_qty == 0:
        return SizingResult(0, None, SizingReason.NO_ORDER_NEEDED, target_shares)

    affordable = _floor_shares(max(Decimal("0"), cash - fee), buffered_price)
    accepted_qty = min(requested_qty, affordable)
    if accepted_qty == 0:
        return SizingResult(requested_qty, None, SizingReason.INSUFFICIENT_CASH, target_shares)
    if accepted_qty < requested_qty:
        return SizingResult(requested_qty, accepted_qty, SizingReason.CASH_SHRUNK, target_shares)
    return SizingResult(requested_qty, accepted_qty, SizingReason.ACCEPTED, target_shares)


def validate_explicit_quantity(
    *,
    side: str,
    quantity: int,
    reference_price: Decimal | None,
    cash_available: Decimal | None,
    fee_reserve: Decimal | None,
    price_buffer_fraction: Decimal | None,
    remaining_strategy_budget: Decimal | None,
    sellable_qty: int | None = None,
    max_order_notional: Decimal | None = None,
    account_fresh: bool = True,
    price_fresh: bool = True,
    position_fresh: bool = True,
) -> SizingResult:
    """Validate an explicit whole-share request without changing its quantity.

    Any failure rejects the entire request. In particular, this helper never
    recomputes a target or shrinks the explicit quantity to fit cash/limits.
    For buys, ``remaining_strategy_budget`` is the caller-computed amount after
    existing strategy exposure and outstanding reservations are deducted.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return SizingResult(quantity if isinstance(quantity, int) else None, None, SizingReason.INVALID_INPUT)
    if side not in {"BUY", "SELL"}:
        return SizingResult(quantity, None, SizingReason.INVALID_INPUT)
    if _missing(reference_price, price_buffer_fraction, fee_reserve):
        return SizingResult(quantity, None, SizingReason.MISSING_INPUT)
    if not price_fresh:
        return SizingResult(quantity, None, SizingReason.STALE_INPUT)
    if not _is_decimal(reference_price) or not _is_decimal(price_buffer_fraction) or not _is_decimal(fee_reserve):
        return SizingResult(quantity, None, SizingReason.INVALID_INPUT)
    if reference_price <= 0 or price_buffer_fraction < 0 or fee_reserve < 0:
        return SizingResult(quantity, None, SizingReason.INVALID_INPUT)

    buffered_price = reference_price * (Decimal("1") + price_buffer_fraction)
    estimated_cost = Decimal(quantity) * buffered_price + fee_reserve
    if side == "BUY":
        if _missing(cash_available):
            return SizingResult(quantity, None, SizingReason.MISSING_INPUT)
        if not account_fresh:
            return SizingResult(quantity, None, SizingReason.STALE_INPUT)
        if not _is_decimal(cash_available) or cash_available < 0:
            return SizingResult(quantity, None, SizingReason.INVALID_INPUT)
        if estimated_cost > cash_available:
            return SizingResult(quantity, None, SizingReason.CASH_LIMIT)
    else:
        if sellable_qty is None:
            return SizingResult(quantity, None, SizingReason.MISSING_INPUT)
        if not position_fresh:
            return SizingResult(quantity, None, SizingReason.STALE_INPUT)
        if isinstance(sellable_qty, bool) or not isinstance(sellable_qty, int) or sellable_qty < 0:
            return SizingResult(quantity, None, SizingReason.INVALID_INPUT)
        if sellable_qty < quantity:
            return SizingResult(quantity, None, SizingReason.INSUFFICIENT_SELLABLE_SHARES)

    limits = ((max_order_notional, SizingReason.ORDER_LIMIT),)
    if side == "BUY":
        if remaining_strategy_budget is None:
            return SizingResult(quantity, None, SizingReason.MISSING_INPUT)
        limits = ((remaining_strategy_budget, SizingReason.BUDGET_LIMIT), *limits)
    for limit, reason in limits:
        if limit is not None:
            if not _is_decimal(limit) or limit < 0:
                return SizingResult(quantity, None, SizingReason.INVALID_INPUT)
            if estimated_cost > limit:
                return SizingResult(quantity, None, reason)
    return SizingResult(quantity, quantity, SizingReason.ACCEPTED)
