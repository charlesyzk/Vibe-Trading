from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.contracts import Bar, Environment, RiskGrant, StrategySpec
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import EventWorkerError
from src.execution.outbox_dispatch import ProposalRiskInputs
from src.execution.risk import PositionFact, RiskFacts
from src.execution.trusted_template_paper import (
    TargetFractionPaperSizer, TrustedTemplateEventCoordinator, TrustedTemplatePaperError,
    _next_rsi_state,
)
from src.execution.versions import StrategyVersionStore


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def _spec(allocation_fraction="0.2") -> StrategySpec:
    return StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "nl2-sma-sizing",
        "source_kind": "natural_language_template", "template_id": "sma_cross", "template_version": "1.0",
        "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
        "parameters": {"fast_period": 2, "slow_period": 5},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                             "allocation_fraction": allocation_fraction, "rebalance": "on_signal_transition",
                             "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "skip", "missed_session_action": "expire"},
    })


def _bar() -> Bar:
    return Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 21),
        start_at=NOW.replace(hour=13, minute=30), end_at=NOW.replace(hour=20),
        available_at=NOW.replace(hour=20), timezone="America/New_York",
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
        volume=Decimal("1000"), currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    )


def _inputs(
    *, quote_as_of=NOW, account_as_of=NOW, strategy_hash="a" * 64,
    available_cash=Decimal("9000"), strategy_reservations=Decimal("0"),
    account_reservations=Decimal("0"),
):
    grant = RiskGrant.model_validate({
        "grant_id": "grant", "user_action_id": "approval", "strategy_version_hash": strategy_hash,
        "broker": "IBKR", "account_ref": "paper-1", "environment": "paper",
        "allowed_instruments": ["AAPL"], "allowed_order_types": ["market"], "long_only": True,
        "whole_shares": True, "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "5000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=1),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })
    facts = RiskFacts(
        broker="IBKR", account_ref="paper-1", environment=Environment.PAPER,
        strategy_version_hash=strategy_hash, instrument_id="AAPL", now=NOW,
        quote_as_of=quote_as_of, account_as_of=account_as_of,
        quote_price=Decimal("100"), price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
        net_liquidation=Decimal("10000"), available_cash=available_cash,
        positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("100")),),
        strategy_position_qty=0, strategy_buy_reservations=strategy_reservations,
        account_buy_reservations=account_reservations, strategy_daily_loss=Decimal("0"),
        orders_this_session=0, account_snapshot_complete=True,
    )
    return ProposalRiskInputs(grant, facts, NOW, NOW + timedelta(minutes=10))


def _size(provider, *, spec=None):
    sizer = TargetFractionPaperSizer(provider)
    return sizer(
        side="BUY", spec=spec or _spec(), run_id="run-1", event_id="event-1", seq=1,
        identity={"account_ref": "paper-1", "environment": "paper", "strategy_version_hash": "a" * 64},
        bar=_bar(),
    )


def test_host_target_fraction_sizer_uses_buffered_price_and_trusted_budget():
    assert _size(lambda _proposal: _inputs()) == 19


def test_strategy_reservation_is_counted_once_for_target_and_cash_limits():
    # One share is already reserved for this strategy and is included in the
    # account-wide $101 reservation total. Cash has $2,100 before reservations.
    inputs = _inputs(
        available_cash=Decimal("2100"), strategy_reservations=Decimal("101"),
        account_reservations=Decimal("101"),
    )
    assert _size(lambda _proposal: inputs, spec=_spec("0.5")) == 19


@pytest.mark.parametrize(
    "inputs, message",
    [
        (_inputs(quote_as_of=NOW - timedelta(seconds=31)), "fresh complete account and quote"),
        (_inputs(account_as_of=NOW - timedelta(seconds=61)), "fresh complete account and quote"),
        (_inputs(strategy_hash="b" * 64), "frozen execution identity"),
    ],
)
def test_host_target_fraction_sizer_fails_closed_on_stale_or_wrong_version_facts(inputs, message):
    with pytest.raises(TrustedTemplatePaperError, match=message):
        _size(lambda _proposal: inputs)


def test_trusted_coordinator_rejects_unpinned_template_bytes_before_event_release(tmp_path):
    from src.bt_runtime.strategies.sma import TRUSTED_TEMPLATE_COMPILER_VERSION

    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), template_source=b"# not the installed compiler source\n",
        engine_version="backtrader/1.9.78.123",
        compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    journal = SQLiteEventJournal(tmp_path / "journal.sqlite")
    journal.create_run(
        run_id="run-1", deployment_id="deployment-1", account_ref="paper-1",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = TrustedTemplateEventCoordinator(
        journal=journal, versions=versions,
        size_signal=TargetFractionPaperSizer(lambda _proposal: _inputs()),
    )

    with pytest.raises(EventWorkerError, match="template source"):
        coordinator.process_next("run-1", next_bar=_bar(), event_id="event-1")

    import sqlite3

    with sqlite3.connect(tmp_path / "journal.sqlite") as db:
        assert db.execute("SELECT next_seq FROM execution_run WHERE run_id='run-1'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM strategy_event WHERE run_id='run-1'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM order_outbox WHERE run_id='run-1'").fetchone() == (0,)


def test_rsi_checkpoint_matches_wilder_seed_and_crosses_entry_then_exit_thresholds():
    state = {}
    sides = []
    params = {"period": 2, "entry_below": 30, "exit_above": 70}
    for close in ("100", "105", "110", "105", "100", "95", "100", "105"):
        state, side = _next_rsi_state(state, Decimal(close), params)
        sides.append(side)

    assert sides == [None, None, None, None, "BUY", None, None, "SELL"]
    assert state["template_id"] == "rsi_threshold"
    assert state["rsi_state"]["seen_closes"] == 8
    assert state["rsi_state"]["warmup_closes"] == []
    assert Decimal(state["rsi_state"]["previous_rsi"]) == pytest.approx(78.125)
