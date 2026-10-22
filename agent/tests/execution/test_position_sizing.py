from decimal import Decimal as D

import pytest

from src.execution.position_sizing import (
    SizingReason,
    size_target_fraction,
    validate_explicit_quantity,
)


def entry(**overrides):
    inputs = {
        "entering_target": True,
        "allocation_fraction": D("0.20"),
        "account_equity": D("10000"),
        "strategy_budget": D("2500"),
        "cash_available": D("5000"),
        "reference_price": D("100"),
        "fee_reserve": D("2"),
        "price_buffer_fraction": D("0.01"),
        "held_qty": 0,
    }
    inputs.update(overrides)
    return size_target_fraction(**inputs)


def test_target_fraction_floors_shares_then_calculates_incremental_buy():
    result = entry(held_qty=5)

    assert result.target_shares == 19  # floor(2,000 / 101)
    assert result.requested_qty == 14
    assert result.accepted_qty == 14
    assert result.reason is SizingReason.ACCEPTED


def test_target_notional_is_capped_by_strategy_budget():
    result = entry(account_equity=D("50000"), strategy_budget=D("500"))

    assert result.target_shares == 4  # floor(500 / 101)
    assert result.accepted_qty == 4


def test_insufficient_cash_shrinks_only_at_decision_and_preserves_request():
    result = entry(cash_available=D("608"))

    assert result.requested_qty == 19
    assert result.accepted_qty == 6  # floor((608 - 2) / 101)
    assert result.reason is SizingReason.CASH_SHRUNK


def test_insufficient_cash_for_one_share_rejects():
    result = entry(cash_available=D("102"))

    assert result.requested_qty == 19
    assert result.accepted_qty is None
    assert result.reason is SizingReason.INSUFFICIENT_CASH


def test_zero_target_shares_does_not_create_order():
    result = entry(account_equity=D("100"), strategy_budget=D("100"))

    assert result.target_shares == 0
    assert result.requested_qty == 0
    assert result.accepted_qty is None
    assert result.reason is SizingReason.ZERO_TARGET_SHARES


def test_exit_quantity_is_strategy_holding_and_cannot_exceed_sellable():
    accepted = size_target_fraction(
        entering_target=False,
        allocation_fraction=None,
        account_equity=None,
        strategy_budget=None,
        cash_available=None,
        reference_price=D("100"),
        fee_reserve=None,
        price_buffer_fraction=D("0"),
        held_qty=8,
        sellable_qty=8,
    )
    rejected = size_target_fraction(
        entering_target=False,
        allocation_fraction=None,
        account_equity=None,
        strategy_budget=None,
        cash_available=None,
        reference_price=D("100"),
        fee_reserve=None,
        price_buffer_fraction=D("0"),
        held_qty=8,
        sellable_qty=7,
    )

    assert accepted.requested_qty == accepted.accepted_qty == 8
    assert rejected.requested_qty == 8
    assert rejected.accepted_qty is None
    assert rejected.reason is SizingReason.INSUFFICIENT_SELLABLE_SHARES


@pytest.mark.parametrize("sellable_qty", [-1, 1.5, True])
def test_target_exit_rejects_invalid_sellable_quantity(sellable_qty):
    result = size_target_fraction(
        entering_target=False,
        allocation_fraction=None,
        account_equity=None,
        strategy_budget=None,
        cash_available=None,
        reference_price=D("100"),
        fee_reserve=None,
        price_buffer_fraction=D("0"),
        held_qty=1,
        sellable_qty=sellable_qty,
    )

    assert result.accepted_qty is None
    assert result.reason is SizingReason.INVALID_INPUT


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"reference_price": None}, SizingReason.MISSING_INPUT),
        ({"account_equity": None}, SizingReason.MISSING_INPUT),
        ({"account_fresh": False}, SizingReason.STALE_INPUT),
        ({"price_fresh": False}, SizingReason.STALE_INPUT),
        ({"position_fresh": False}, SizingReason.STALE_INPUT),
        ({"reference_price": D("0")}, SizingReason.INVALID_INPUT),
        ({"cash_available": D("-1")}, SizingReason.INVALID_INPUT),
    ],
)
def test_missing_stale_and_invalid_inputs_block(overrides, expected):
    result = entry(**overrides)

    assert result.accepted_qty is None
    assert result.reason is expected


def test_explicit_quantity_is_passed_through_without_cash_shrink():
    result = validate_explicit_quantity(
        side="BUY",
        quantity=4,
        reference_price=D("100"),
        cash_available=D("301"),
        fee_reserve=D("1"),
        price_buffer_fraction=D("0"),
        remaining_strategy_budget=D("1000"),
    )

    assert result.requested_qty == 4
    assert result.accepted_qty is None
    assert result.reason is SizingReason.CASH_LIMIT


def test_explicit_quantity_accepts_original_quantity_when_valid():
    result = validate_explicit_quantity(
        side="SELL",
        quantity=3,
        reference_price=D("100"),
        cash_available=None,
        fee_reserve=D("1"),
        price_buffer_fraction=D("0.01"),
        remaining_strategy_budget=D("1000"),
        sellable_qty=3,
    )

    assert result.requested_qty == result.accepted_qty == 3
    assert result.reason is SizingReason.ACCEPTED


def test_explicit_quantity_rejects_missing_or_stale_facts():
    missing = validate_explicit_quantity(
        side="BUY",
        quantity=4,
        reference_price=D("100"),
        cash_available=None,
        fee_reserve=D("1"),
        price_buffer_fraction=D("0"),
        remaining_strategy_budget=D("1000"),
    )
    stale = validate_explicit_quantity(
        side="BUY",
        quantity=4,
        reference_price=D("100"),
        cash_available=D("1000"),
        fee_reserve=D("1"),
        price_buffer_fraction=D("0"),
        remaining_strategy_budget=D("1000"),
        account_fresh=False,
    )

    assert missing.reason is SizingReason.MISSING_INPUT
    assert stale.reason is SizingReason.STALE_INPUT
    assert missing.accepted_qty is stale.accepted_qty is None


def test_explicit_buy_rejects_when_remaining_budget_is_below_full_request():
    result = validate_explicit_quantity(
        side="BUY",
        quantity=4,
        reference_price=D("100"),
        cash_available=D("1000"),
        fee_reserve=D("1"),
        price_buffer_fraction=D("0"),
        remaining_strategy_budget=D("350"),
    )

    assert result.requested_qty == 4
    assert result.accepted_qty is None
    assert result.reason is SizingReason.BUDGET_LIMIT


@pytest.mark.parametrize("sellable_qty", [-1, 1.5, True])
def test_explicit_sell_rejects_invalid_sellable_quantity(sellable_qty):
    result = validate_explicit_quantity(
        side="SELL",
        quantity=1,
        reference_price=D("100"),
        cash_available=None,
        fee_reserve=D("1"),
        price_buffer_fraction=D("0"),
        remaining_strategy_budget=None,
        sellable_qty=sellable_qty,
    )

    assert result.accepted_qty is None
    assert result.reason is SizingReason.INVALID_INPUT
