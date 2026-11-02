from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.execution.contracts import RiskGrant
from src.execution.ibkr_paper_risk_facts import (
    IbkrLocalPaperQuoteReader,
    IbkrPaperRiskFactsError,
    IbkrPaperRiskFactsProvider,
    PaperOrderPolicy,
    StrategyRiskMetrics,
    VerifiedIbkrContract,
)
from src.execution.outbox_dispatch import OutboxProposal, RiskAwareProposalAuthorizer
from src.execution.risk import ProposedOrder
from tests.execution.test_outbox_dispatch import _bar
from src.trading.connectors.ibkr.local import IBKRLocalConfig


NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
SIGNAL_END = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
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


def make_provider(*, snapshot=None, response=None, metrics=None, order_policy=None):
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
        order_policy=order_policy,
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


@pytest.mark.parametrize("cash, funds, expected", [
    ("300", "4500", Decimal("300")),
    ("5000", "4500", Decimal("4500")),
])
def test_available_cash_excludes_margin_buying_power(cash, funds, expected):
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(), "positions": [], "open_orders": [],
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
            {"tag": "TotalCashValue", "currency": "USD", "value": cash},
            {"tag": "AvailableFunds", "currency": "USD", "value": funds},
        ],
    }
    assert make_provider(snapshot=snapshot).build(intent(), make_grant()).available_cash == expected


def test_negative_cash_does_not_authorize_margin_funded_orders():
    with pytest.raises(IbkrPaperRiskFactsError, match="invalid net liquidation or available funds"):
        IbkrPaperRiskFactsProvider._account_values([
            {"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
            {"tag": "TotalCashValue", "currency": "USD", "value": "-1"},
            {"tag": "AvailableFunds", "currency": "USD", "value": "4500"},
        ])


@pytest.mark.parametrize("change, reason", [
    ({"open_orders": [{"order_id": 42}]}, "reservation reconciliation"),
    ({"facts_sha256": "not-a-digest"}, "integrity digest"),
    ({"facts_sha256": None}, "integrity digest"),
])
def test_rejects_unaccounted_broker_orders_or_invalid_snapshot_digest(change, reason):
    snapshot = {
        "capture_complete": True, "reconciled": True, "account_ref": "DU123",
        "environment": "paper", "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(), "open_orders": [],
    }
    snapshot.update(change)
    with pytest.raises(IbkrPaperRiskFactsError, match=reason):
        make_provider(snapshot=snapshot).build(intent(), make_grant())


def test_policy_must_match_grant_and_per_order_limit():
    order = ProposedOrder(instrument_id="AAPL", side="BUY", integer_qty=3,
                          order_type="limit", time_in_force="DAY", limit_price=Decimal("200"))
    with pytest.raises(ValueError, match="notional"):
        make_provider(order_policy=PaperOrderPolicy("DU123", VERSION, order))
    with pytest.raises(ValueError, match="risk grant"):
        make_provider(order_policy=PaperOrderPolicy("DU999", VERSION, order.model_copy(update={"integer_qty": 1})))
    with pytest.raises(ValueError, match="PAPER account"):
        PaperOrderPolicy("LIVE", VERSION, order)


@pytest.mark.parametrize("symbol,quantity", [("AAPL", 1), ("MSFT", 5)])
def test_policy_binds_instrument_quantity_and_limit_to_frozen_version(tmp_path, symbol, quantity):
    from datetime import date
    import backtrader
    from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
    from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
    from src.execution.versions import StrategyVersionStore

    spec, source = build_paper_integration_test_spec(date(2026, 10, 30))
    payload = spec.model_dump(mode="json")
    payload["instrument_ids"] = [symbol]
    payload["parameters"]["quantity"] = quantity
    version = StrategyVersionStore(tmp_path / "versions").create_version(
        type(spec).model_validate(payload), upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}", compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    order = ProposedOrder(instrument_id=symbol, side="BUY", integer_qty=quantity,
                          order_type="limit", time_in_force="DAY", limit_price=Decimal("400"))
    policy = PaperOrderPolicy.from_frozen_version(account_ref="DU123", version=version, order=order)
    assert policy.strategy_version_hash == version.version_id
    for changed in (
        order.model_copy(update={"instrument_id": "OTHER"}),
        order.model_copy(update={"integer_qty": quantity + 1}),
        order.model_copy(update={"limit_price": Decimal("399")}),
        order.model_copy(update={"side": "SELL"}),
    ):
        with pytest.raises(ValueError, match="differs from frozen"):
            PaperOrderPolicy.from_frozen_version(account_ref="DU123", version=version, order=changed)


@pytest.mark.parametrize("section,field,value", [
    ("bar_policy", "calendar_id", "other_calendar"),
    ("bar_policy", "timezone", "UTC"),
    ("execution_policy", "gap_action", "skip"),
    ("execution_policy", "missed_session_action", "skip"),
])
def test_policy_rejects_runtime_incompatible_frozen_schedule(tmp_path, section, field, value):
    from datetime import date
    import backtrader
    from pydantic import ValidationError
    from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
    from src.execution.paper_integration_test_strategy import build_paper_integration_test_spec
    from src.execution.versions import StrategyVersionStore

    spec, source = build_paper_integration_test_spec(date(2026, 10, 30))
    data = spec.model_dump(mode="json")
    data[section][field] = value
    try:
        changed_spec = type(spec).model_validate(data)
    except ValidationError:
        # A contract-level rejection is an equally valid fail-closed result.
        return
    version = StrategyVersionStore(tmp_path / "versions").create_version(
        changed_spec, upload_source=source, engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    order = ProposedOrder(instrument_id="AAPL", side="BUY", integer_qty=1,
                          order_type="limit", time_in_force="DAY", limit_price=Decimal("400"))
    with pytest.raises(ValueError, match="differs from frozen"):
        PaperOrderPolicy.from_frozen_version(account_ref="DU123", version=version, order=order)


@pytest.mark.parametrize("symbol,con_id,quantity,limit", [
    ("AAPL", 265598, 1, "200"), ("MSFT", 272093, 5, "200"),
])
def test_generic_proposal_uses_live_scope_bound_facts_and_enforces_limits(symbol, con_id, quantity, limit):
    provider = make_provider()
    provider.risk_grant = make_grant(
        allowed_instruments=[symbol], max_order_notional="1200",
        max_gross_exposure="1200", strategy_budget="1200",
    )
    provider.contracts = {symbol: VerifiedIbkrContract(symbol, symbol, con_id)}
    provider.by_con_id = {con_id: provider.contracts[symbol]}
    provider.order_policy = PaperOrderPolicy(
        account_ref="DU123", strategy_version_hash=VERSION,
        order=ProposedOrder(instrument_id=symbol, side="BUY", integer_qty=quantity,
                            order_type="limit", time_in_force="DAY", limit_price=Decimal(limit)),
    )
    response = quote()
    response["qualified_contract"].update(symbol=symbol, con_id=con_id)
    provider.quote_reader = lambda _contract: response
    proposal = OutboxProposal(
        run_id="run", event_id="event", seq=1, proposal_index=0,
        proposal={"side": "BUY", "integer_qty": quantity, "order_type": "limit",
                  "time_in_force": "DAY", "limit_price": limit},
        identity={"account_ref": "DU123", "environment": "paper",
                  "strategy_version_hash": VERSION, "deployment_id": "deployment"},
        bar=_bar().model_copy(update={"instrument_id": symbol, "end_at": SIGNAL_END}),
    )
    authorizer = RiskAwareProposalAuthorizer(provider.proposal_inputs)
    accepted = authorizer(proposal)
    assert accepted.decision.allowed
    assert accepted.intent.instrument_id == symbol
    assert accepted.intent.integer_qty == quantity
    short_grant = provider.risk_grant
    provider.risk_grant = short_grant.model_copy(update={"valid_until": NOW + timedelta(days=3)})
    assert provider.proposal_inputs(proposal).expires_at == datetime(2026, 11, 2, 21, tzinfo=timezone.utc)
    stale = replace(proposal, bar=proposal.bar.model_copy(update={"end_at": SIGNAL_END - timedelta(days=1)}))
    with pytest.raises(IbkrPaperRiskFactsError, match="stale or outside"):
        provider.proposal_inputs(stale)
    provider.risk_grant = short_grant

    oversized = replace(proposal, proposal={**proposal.proposal, "integer_qty": 7})
    with pytest.raises(IbkrPaperRiskFactsError, match="order does not match"):
        authorizer(oversized)

    wrong_side = replace(proposal, proposal={**proposal.proposal, "side": "SELL"})
    with pytest.raises(IbkrPaperRiskFactsError, match="order does not match"):
        authorizer(wrong_side)
    wrong_price = replace(proposal, proposal={**proposal.proposal, "limit_price": "201"})
    with pytest.raises(IbkrPaperRiskFactsError, match="order does not match"):
        authorizer(wrong_price)

    dispatch_intent = SimpleNamespace(**{
        "broker": "IBKR", "account_ref": "DU123", "environment": provider.risk_grant.environment,
        "strategy_version_hash": VERSION, "deployment_id": "deployment", "instrument_id": symbol, "bar_end": SIGNAL_END,
        **proposal.proposal,
    })
    assert provider(dispatch_intent, excluding_intent_id="fixture").facts.instrument_id == symbol
    dispatch_intent.bar_end = SIGNAL_END - timedelta(days=1)
    with pytest.raises(IbkrPaperRiskFactsError, match="stale or outside"):
        provider(dispatch_intent, excluding_intent_id="fixture")
    dispatch_intent.bar_end = SIGNAL_END
    dispatch_intent.integer_qty = quantity + 1
    with pytest.raises(IbkrPaperRiskFactsError, match="order does not match"):
        provider(dispatch_intent, excluding_intent_id="fixture")
    provider.order_policy = None
    with pytest.raises(IbkrPaperRiskFactsError, match="explicit PAPER order policy"):
        authorizer(proposal)
    provider.order_policy = PaperOrderPolicy("DU123", VERSION, ProposedOrder(
        instrument_id=symbol, side="BUY", integer_qty=quantity,
        order_type="limit", time_in_force="DAY", limit_price=Decimal(limit),
    ))

    wrong_scope = replace(proposal, identity={**proposal.identity, "account_ref": "DU999"})
    with pytest.raises(IbkrPaperRiskFactsError, match="identity"):
        authorizer(wrong_scope)
    original_grant = provider.risk_grant
    provider.risk_grant = original_grant.model_copy(update={"granted_at": NOW + timedelta(minutes=1)})
    with pytest.raises(IbkrPaperRiskFactsError, match="valid window"):
        authorizer(proposal)
    provider.risk_grant = original_grant
    provider.now = lambda: NOW + timedelta(hours=2)
    with pytest.raises(IbkrPaperRiskFactsError, match="valid window"):
        authorizer(proposal)


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
            {"tag": "TotalCashValue", "currency": "HKD", "value": "31200"},
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
    assert facts.available_cash == Decimal("4000")


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


def test_provider_uses_ledger_strategy_position_and_compares_broker_position(tmp_path):
    from src.execution.order_ledger import BrokerEventKind, SQLiteOrderLedger
    from src.execution.paper_strategy_metrics import LedgerStrategyMetricsReader
    from tests.execution.test_order_ledger import intent as ledger_intent, report

    ledger = SQLiteOrderLedger(tmp_path / "paper.sqlite")
    order = ledger.create_intent(ledger_intent(broker="IBKR", account_ref="DU123",
                                              strategy_version_hash=VERSION, instrument_id="AAPL"))
    ledger.reserve(order.intent_id, "r", "300")
    ledger.start_send(order.intent_id, attempt_id="a", client_order_id="c")
    # Fill precedes the account snapshot; submission count is separately
    # excluded here because this fixture uses a historical clock.
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.FILL, "event", broker="IBKR",
                                      account_ref="DU123", broker_exec_id="exec", quantity=3, price=Decimal("10")))
    snapshot = {"capture_complete": True, "reconciled": True, "account_ref": "DU123",
                "environment": "paper", "facts_sha256": "b" * 64, "completed_at_utc": NOW.isoformat(),
                "account_summary": [{"tag": "NetLiquidation", "currency": "USD", "value": "10000"},
                                    {"tag": "TotalCashValue", "currency": "USD", "value": "5000"},
                                    {"tag": "AvailableFunds", "currency": "USD", "value": "4500"}],
                "positions": [{"contract_id": 265598, "symbol": "AAPL", "currency": "USD", "quantity": "3"}],
                "open_orders": []}
    provider = make_provider(snapshot=snapshot)
    reader = LedgerStrategyMetricsReader(database=ledger.path, account_ref="DU123", source=lambda *_: None)
    class Metrics:
        def __call__(self, *_):
            return StrategyRiskMetrics(0, 0, Decimal("0"), Decimal("0"), 0, Decimal("0.05"), Decimal("1"))
        position_at = reader.position_at
    provider.metrics_reader = Metrics()
    assert provider.build(intent(), make_grant()).strategy_position_qty == 3
    snapshot["positions"][0]["quantity"] = "2"
    with pytest.raises(IbkrPaperRiskFactsError, match="exceeds reconciled account position"):
        provider.build(intent(), make_grant())
