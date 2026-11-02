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


def test_grant_can_be_issued_before_window_but_never_used_early_or_before_issue():
    start = NOW + timedelta(hours=1)
    grant = make_grant(granted_at=NOW, valid_from=start, valid_until=start + timedelta(hours=1))
    order = ProposedOrder(instrument_id="AAPL", side="BUY", integer_qty=1)
    assert "grant_expired_or_not_yet_valid" in evaluate_order_risk(grant, order, make_facts()).reason_codes
    at_open = make_facts(now=start, quote_as_of=start, account_as_of=start)
    assert evaluate_order_risk(grant, order, at_open).allowed
    at_close = make_facts(now=grant.valid_until, quote_as_of=grant.valid_until, account_as_of=grant.valid_until)
    assert "grant_expired_or_not_yet_valid" in evaluate_order_risk(grant, order, at_close).reason_codes
    future_issued = make_grant(granted_at=NOW + timedelta(minutes=1))
    assert "grant_not_yet_issued" in evaluate_order_risk(future_issued, order, make_facts()).reason_codes


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


def test_limit_order_risk_uses_the_exact_limit_as_a_hard_notional_ceiling():
    grant = make_grant(allowed_order_types=["limit"], max_order_notional="400")
    order = ProposedOrder(
        instrument_id="AAPL", side="BUY", integer_qty=1,
        order_type="limit", limit_price=Decimal("400"),
    )
    decision = evaluate_order_risk(grant, order, make_facts(quote_price=Decimal("500")))
    assert decision.allowed is True
    assert decision.estimated_notional == Decimal("400")
    assert decision.estimated_cost == Decimal("402")

    capped = evaluate_order_risk(
        make_grant(allowed_order_types=["limit"], max_order_notional="399"),
        order,
        make_facts(),
    )
    assert capped.allowed is False
    assert "max_order_notional" in capped.reason_codes


def test_broker_enforced_limit_mode_is_bound_to_the_single_aapl_paper_envelope():
    grant = make_grant(
        allowed_order_types=["limit"],
        max_order_notional="400", max_gross_exposure="400", strategy_budget="400",
        max_daily_loss="400", max_orders_per_session=1, quote_max_age_seconds=10,
        account_max_age_seconds=10, account_ref="DU123456", funds_check="broker_paper_limit",
    )
    facts = make_facts(
        quote_as_of=None, quote_price=None, net_liquidation=None, available_cash=None,
        positions=(), strategy_position_qty=0, strategy_sell_reservations=0,
        strategy_buy_reservations=Decimal("0"), account_buy_reservations=Decimal("0"),
        strategy_daily_loss=Decimal("0"), orders_this_session=0,
        estimated_fees=Decimal("1"),
        account_ref="DU123456", price_buffer_fraction=Decimal("0.05"),
    )
    order = ProposedOrder(
        instrument_id="AAPL", side="BUY", integer_qty=1,
        order_type="limit", limit_price=Decimal("400"),
    )

    decision = evaluate_order_risk(grant, order, facts)

    assert decision.allowed is True
    assert decision.estimated_notional == Decimal("400")
    assert decision.estimated_cost == Decimal("401")
    assert decision.projected_strategy_exposure == Decimal("400")
    assert decision.projected_gross_exposure == Decimal("400")


def test_broker_enforced_limit_mode_rejects_other_orders_and_nonempty_account_state():
    grant = make_grant(
        allowed_order_types=["limit"],
        max_order_notional="400", max_gross_exposure="400", strategy_budget="400",
        max_daily_loss="400", max_orders_per_session=1, quote_max_age_seconds=10,
        account_max_age_seconds=10, account_ref="DU123456", funds_check="broker_paper_limit",
    )
    facts = make_facts(
        quote_as_of=None, quote_price=None, net_liquidation=None, available_cash=None,
        positions=(), strategy_position_qty=0, strategy_sell_reservations=0,
        strategy_buy_reservations=Decimal("0"), account_buy_reservations=Decimal("0"),
        strategy_daily_loss=Decimal("0"), orders_this_session=0,
        estimated_fees=Decimal("1"),
        account_ref="DU123456", price_buffer_fraction=Decimal("0.05"),
    )
    result = evaluate_order_risk(
        grant,
        {"instrument_id": "AAPL", "side": "BUY", "integer_qty": 2,
         "order_type": "limit", "limit_price": "400"},
        facts,
    )
    assert result.allowed is False
    assert "broker_limit_test_envelope_mismatch" in result.reason_codes

    with pytest.raises(ValueError, match="exact one-shot AAPL PAPER envelope"):
        make_grant(
            allowed_order_types=["market"], max_order_notional="400", max_gross_exposure="400",
            strategy_budget="400", max_daily_loss="400", max_orders_per_session=1,
            quote_max_age_seconds=10, account_max_age_seconds=10, account_ref="DU123456",
            funds_check="broker_paper_limit",
        )

    with pytest.raises(ValueError, match="exact one-shot AAPL PAPER envelope"):
        make_grant(
            broker="FAKE", allowed_order_types=["limit"], max_order_notional="400",
            max_gross_exposure="400", strategy_budget="400", max_daily_loss="400",
            max_orders_per_session=1, quote_max_age_seconds=10, account_max_age_seconds=10,
            account_ref="DU123456", funds_check="broker_paper_limit",
        )


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
