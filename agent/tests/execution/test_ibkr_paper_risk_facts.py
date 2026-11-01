from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.execution.contracts import RiskGrant
from src.execution.ibkr_paper_risk_facts import (
    IbkrLocalPaperQuoteReader,
    IbkrPaperRiskFactsError,
    IbkrPaperRiskFactsProvider,
    StrategyRiskMetrics,
    VerifiedIbkrContract,
)
from src.trading.connectors.ibkr.local import IBKRLocalConfig


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
VERSION = "a" * 64


def make_grant(**changes) -> RiskGrant:
    values = {
        "grant_id": "grant", "user_action_id": "action", "strategy_version_hash": VERSION,
        "broker": "IBKR", "account_ref": "DU123", "environment": "paper",
        "allowed_instruments": ["AAPL"], "allowed_order_types": ["limit"],
        "long_only": True, "whole_shares": True, "max_order_notional": "400",
        "max_gross_exposure": "400", "strategy_budget": "400", "max_daily_loss": "400",
        "max_orders_per_session": 1, "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(hours=1), "quote_max_age_seconds": 10,
        "account_max_age_seconds": 30, "halt_action": "pause_and_cancel_owned",
        "liquidation_policy": "none", "granted_at": NOW,
    }
    values.update(changes)
    return RiskGrant.model_validate(values)


def quote(*, tier=1, bid="199.90", ask="200.10", observed=NOW):
    return {
        "status": "ok", "market_data_type_actual": "live" if tier == 1 else "delayed",
        "market_data_type_actual_code": tier,
        "execution_ready": tier == 1, "quote_quality": "two_sided",
        "observed_at_utc": observed.isoformat(),
        "qualified_contract": {
            "symbol": "AAPL", "sec_type": "STK", "exchange": "SMART",
            "primary_exchange": "NASDAQ", "currency": "USD", "con_id": 265598,
        },
        "quote": {"bid": bid, "ask": ask},
    }


def make_provider(*, snapshot=None, response=None, metrics=None):
    return IbkrPaperRiskFactsProvider(
        account_ref="DU123", strategy_version_hash=VERSION, risk_grant=make_grant(),
        contracts={"AAPL": VerifiedIbkrContract("AAPL", "AAPL", 265598)},
        fx_contract_ids={},
        snapshot_reader=lambda: snapshot or {
            "capture_complete": True, "reconciled": True, "account_ref": "DU123",
            "environment": "paper", "facts_sha256": "b" * 64,
            "completed_at_utc": NOW.isoformat(),
            "account_summary": [
                {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
                {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
                {"tag": "AvailableFunds", "currency": "USD", "value": "4500"},
            ],
            "positions": [], "open_orders": [],
        },
        quote_reader=lambda _contract: response or quote(),
        fx_quote_reader=lambda _currency: {},
        metrics_reader=lambda *_: metrics or StrategyRiskMetrics(
            strategy_position_qty=0, strategy_sell_reservations=0,
            strategy_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
            orders_this_session=0, price_buffer_fraction=Decimal("0.05"),
            estimated_fees=Decimal("1"),
        ),
        now=lambda: NOW,
    )


def intent(**changes):
    values = {
        "broker": "IBKR", "account_ref": "DU123", "environment": make_grant().environment,
        "strategy_version_hash": VERSION, "instrument_id": "AAPL", "side": "BUY",
        "deployment_id": "deployment",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_local_quote_reader_pins_paper_account_requests_live_and_keeps_readonly(monkeypatch):
    from src.trading.connectors.ibkr import local

    calls = []

    def fake_get_quote(symbol, *, config, exchange, currency, sec_type):
        calls.append((symbol, config, exchange, currency, sec_type))
        return {"status": "ok"}

    monkeypatch.setattr(local, "get_quote", fake_get_quote)
    reader = IbkrLocalPaperQuoteReader(
        config=IBKRLocalConfig(
            profile="paper", host="127.0.0.1", port=4002,
            account="DU123", readonly=True, market_data_type=3,
        ),
        account_ref="DU123",
    )
    contract = VerifiedIbkrContract("AAPL", "AAPL", 265598)

    reader(contract)
    reader.fx_quote("HKD")

    assert [call[0] for call in calls] == ["AAPL", "USD"]
    assert all(call[1].market_data_type == 1 and call[1].readonly is True for call in calls)
    assert calls[0][2:] == ("SMART", "USD", "STK")
    assert calls[1][2:] == ("IDEALPRO", "HKD", "CASH")


@pytest.mark.parametrize("config", [
    IBKRLocalConfig(profile="live-readonly", host="127.0.0.1", port=4001, account="DU123"),
    IBKRLocalConfig(profile="paper", host="0.0.0.0", port=4002, account="DU123"),
    IBKRLocalConfig(profile="paper", host="127.0.0.1", port=4002, account="DU999"),
    IBKRLocalConfig(profile="paper", host="127.0.0.1", port=4002, account="DU123", readonly=False),
])
def test_local_quote_reader_rejects_wrong_environment_or_account(config):
    with pytest.raises(ValueError, match="loopback PAPER read-only"):
        IbkrLocalPaperQuoteReader(config=config, account_ref="DU123")


def test_builds_facts_from_a_fresh_reconciled_snapshot_and_live_verified_quote():
    facts = make_provider().build(intent(), make_grant())
    assert facts.quote_price == Decimal("200.10")
    assert facts.available_cash == Decimal("4500")
    assert facts.net_liquidation == Decimal("10000")
    assert facts.account_snapshot_complete is True
    assert facts.account_buy_reservations == 0


@pytest.mark.parametrize("snapshot_change", [
    {"reconciled": False},
    {"account_ref": "DU999"},
])
def test_refuses_unreconciled_or_wrong_account_snapshot(snapshot_change):
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(),
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
            {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
            {"tag": "AvailableFunds", "currency": "USD", "value": "4500"},
        ], "positions": [], "open_orders": [],
    }
    snapshot.update(snapshot_change)
    with pytest.raises(IbkrPaperRiskFactsError):
        make_provider(snapshot=snapshot).build(intent(), make_grant())


def test_refuses_delayed_quote_even_with_valid_prices():
    with pytest.raises(IbkrPaperRiskFactsError, match="live two-sided"):
        make_provider(response=quote(tier=3)).build(intent(), make_grant())


def test_refuses_stale_account_snapshot():
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": (NOW - timedelta(minutes=2)).isoformat(),
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
            {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
            {"tag": "AvailableFunds", "currency": "USD", "value": "4500"},
        ], "positions": [], "open_orders": [],
    }
    with pytest.raises(IbkrPaperRiskFactsError, match="stale or from the future"):
        make_provider(snapshot=snapshot).build(intent(), make_grant())


def test_converts_base_currency_to_usd_only_through_pinned_live_fx_contract():
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(),
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "HKD", "value": "78000"},
            {"tag": "TotalCashValue", "currency": "HKD", "value": "39000"},
            {"tag": "AvailableFunds", "currency": "HKD", "value": "35100"},
        ], "positions": [], "open_orders": [],
    }
    fx = {
        "status": "ok", "market_data_type_actual": "live", "market_data_type_actual_code": 1,
        "execution_ready": True, "quote_quality": "two_sided", "observed_at_utc": NOW.isoformat(),
        "qualified_contract": {
            "symbol": "USD", "sec_type": "CASH", "exchange": "IDEALPRO",
            "primary_exchange": "", "currency": "HKD", "con_id": 1001,
        }, "quote": {"bid": "7.79", "ask": "7.80"},
    }
    provider = IbkrPaperRiskFactsProvider(
        account_ref="DU123", strategy_version_hash=VERSION, risk_grant=make_grant(),
        contracts={"AAPL": VerifiedIbkrContract("AAPL", "AAPL", 265598)},
        fx_contract_ids={"HKD": 1001},
        snapshot_reader=lambda: snapshot,
        quote_reader=lambda _contract: quote(),
        fx_quote_reader=lambda _currency: fx,
        metrics_reader=lambda *_: StrategyRiskMetrics(
            strategy_position_qty=0, strategy_sell_reservations=0,
            strategy_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
            orders_this_session=0, price_buffer_fraction=Decimal("0.05"),
            estimated_fees=Decimal("1"),
        ),
        now=lambda: NOW,
    )
    facts = provider.build(intent(), make_grant())
    assert facts.net_liquidation == Decimal("10000")
    assert facts.available_cash == Decimal("4500")


def test_refuses_unmapped_broker_positions():
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(),
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
            {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
            {"tag": "AvailableFunds", "currency": "USD", "value": "4500"},
        ], "positions": [{"contract_id": 999, "symbol": "XYZ", "currency": "USD", "quantity": "1"}],
        "open_orders": [],
    }
    with pytest.raises(IbkrPaperRiskFactsError, match="verified contract mapping"):
        make_provider(snapshot=snapshot).build(intent(), make_grant())
