from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.contracts import Environment, RiskGrant
from src.execution.risk import ProposedOrder, RiskFacts, evaluate_order_risk

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
VERSION = "a" * 64


def make_grant(**changes) -> RiskGrant:
    value = {
        "grant_id": "grant-fixture", "user_action_id": "action-fixture",
        "strategy_version_hash": VERSION, "broker": "IBKR", "account_ref": "paper-fixture",
        "environment": "paper", "allowed_instruments": ["AAPL"], "allowed_order_types": ["market"],
        "long_only": True, "whole_shares": True,
        "max_order_notional": "1000", "max_gross_exposure": "30000",
        "strategy_budget": "3000", "max_daily_loss": "500", "max_orders_per_session": 5,
        "valid_from": NOW - timedelta(hours=1), "valid_until": NOW + timedelta(hours=1),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    }
    value.update(changes)
    return RiskGrant.model_validate(value)


def make_facts(**changes) -> RiskFacts:
    value = {
        "broker": "IBKR", "account_ref": "paper-fixture", "environment": Environment.PAPER,
        "strategy_version_hash": VERSION, "instrument_id": "AAPL", "now": NOW,
        "quote_as_of": NOW - timedelta(seconds=2), "account_as_of": NOW - timedelta(seconds=3),
        "quote_price": Decimal("100"), "price_buffer_fraction": Decimal("0.01"),
        "estimated_fees": Decimal("2"), "net_liquidation": Decimal("10000"),
        "available_cash": Decimal("5000"),
        "positions": ({"instrument_id": "AAPL", "quantity": 20, "market_price": "100"},),
        "strategy_position_qty": 5, "strategy_buy_reservations": Decimal("100"),
        "account_buy_reservations": Decimal("200"), "strategy_daily_loss": Decimal("0"),
        "orders_this_session": 1, "account_snapshot_complete": True,
    }
    value.update(changes)
    return RiskFacts.model_validate(value)


def test_risk_allows_exact_request_and_binds_a_deterministic_snapshot():
    grant = make_grant()
    order = ProposedOrder(instrument_id="AAPL", side="BUY", integer_qty=4)
    facts = make_facts()

    first = evaluate_order_risk(grant, order, facts)
    second = evaluate_order_risk(grant, order, facts)

    assert first.allowed is True
    assert first.reason_codes == ()
    assert first.estimated_notional == Decimal("400")
    assert first.estimated_cost == Decimal("406.00")
    assert first.projected_strategy_exposure == Decimal("1004.00")
    assert first.projected_gross_exposure == Decimal("2604.00")
    assert first.risk_snapshot_id == second.risk_snapshot_id


def test_risk_counts_existing_strategy_exposure_and_all_account_reservations():
    grant = make_grant(strategy_budget="900")
    decision = evaluate_order_risk(
        grant,
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 4},
        make_facts(),
    )

    assert decision.allowed is False
    assert "strategy_budget" in decision.reason_codes


def test_risk_applies_price_buffer_to_order_and_gross_limits():
    decision = evaluate_order_risk(
        make_grant(max_order_notional="400", max_gross_exposure="2600"),
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 4},
        make_facts(),
    )

    assert decision.allowed is False
    assert {"max_order_notional", "max_gross_exposure"}.issubset(decision.reason_codes)


def test_risk_fails_closed_for_identity_staleness_incomplete_daily_loss_and_cash():
    grant = make_grant()
    order = {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 4}
    facts = make_facts(
        account_ref="wrong-account",
        quote_as_of=NOW - timedelta(minutes=1),
        account_snapshot_complete=False,
        strategy_daily_loss=Decimal("500"),
        available_cash=Decimal("300"),
    )

    decision = evaluate_order_risk(grant, order, facts)

    assert decision.allowed is False
    assert {
        "account_mismatch", "quote_stale_or_future", "account_snapshot_incomplete",
        "daily_loss_limit", "available_cash",
    }.issubset(decision.reason_codes)


def test_risk_rejects_revoked_grants_and_wrong_version_bindings():
    grant = make_grant(revoked_at=NOW)
    facts = make_facts(strategy_version_hash="b" * 64)

    decision = evaluate_order_risk(
        grant,
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 4},
        facts,
    )

    assert decision.allowed is False
    assert {"grant_revoked", "strategy_version_mismatch"}.issubset(decision.reason_codes)


def test_risk_never_shrinks_explicit_sell_and_rejects_quantity_over_owned():
    decision = evaluate_order_risk(
        make_grant(),
        {"instrument_id": "AAPL", "side": "SELL", "integer_qty": 6},
        make_facts(),
    )

    assert decision.allowed is False
    assert "sell_exceeds_strategy_position" in decision.reason_codes
    assert decision.estimated_notional == Decimal("600")


def test_proposed_order_rejects_non_market_day_and_non_integer_quantity():
    for candidate in (
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": True},
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 1, "order_type": "limit"},
    ):
        with pytest.raises(ValueError):
            ProposedOrder.model_validate(candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("price_buffer_fraction", Decimal("-0.01")),
        ("estimated_fees", Decimal("-0.01")),
    ],
)
def test_risk_facts_reject_negative_cost_inputs(field, value):
    with pytest.raises(ValueError, match="non-negative"):
        make_facts(**{field: value})
