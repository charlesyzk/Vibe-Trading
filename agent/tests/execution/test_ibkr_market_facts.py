from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.ibkr_market_facts import (
    IbkrExecutionQuote,
    IbkrExecutionQuoteError,
    convert_to_usd,
)


NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def _response(**changes):
    response = {
        "status": "ok",
        "market_data_type_actual": "live",
        "market_data_type_actual_code": 1,
        "execution_ready": True,
        "quote_quality": "two_sided",
        "observed_at_utc": (NOW - timedelta(seconds=1)).isoformat(),
        "qualified_contract": {
            "symbol": "AAPL",
            "con_id": 265598,
            "sec_type": "STK",
            "exchange": "SMART",
            "primary_exchange": "NASDAQ",
            "currency": "USD",
        },
        "quote": {"bid": "200.10", "ask": "200.20", "last": "200.15"},
    }
    response.update(changes)
    return response


def _parse(response):
    return IbkrExecutionQuote.from_connector_response(
        response,
        expected_symbol="AAPL",
        expected_con_id=265598,
        expected_security_type="STK",
        expected_exchange="SMART",
        expected_primary_exchange="NASDAQ",
        expected_currency="USD",
        now=NOW,
        max_age_seconds=10,
    )


def test_quote_requires_live_tier_and_exact_verified_contract():
    quote = _parse(_response())
    assert quote.execution_price("BUY") == Decimal("200.20")
    assert quote.execution_price("SELL") == Decimal("200.10")
    assert quote.con_id == 265598


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_ready": False},
        {"quote_quality": "last_only"},
    ],
)
def test_quote_rejects_live_tier_without_execution_ready_two_sided_market(changes):
    with pytest.raises(IbkrExecutionQuoteError, match="two-sided"):
        _parse(_response(**changes))


@pytest.mark.parametrize("tier,code", [("delayed", 3), ("delayed-frozen", 4), ("frozen", 2), (None, None)])
def test_quote_rejects_non_live_or_unreported_tier(tier, code):
    with pytest.raises(IbkrExecutionQuoteError, match="not confirmed live"):
        _parse(_response(market_data_type_actual=tier, market_data_type_actual_code=code))


@pytest.mark.parametrize(
    "field,value",
    [
        ("con_id", 999),
        ("currency", "HKD"),
        ("symbol", "MSFT"),
        ("sec_type", "OPT"),
        ("exchange", "NYSE"),
    ],
)
def test_quote_rejects_contract_mapping_mismatch(field, value):
    contract = dict(_response()["qualified_contract"])
    contract[field] = value
    with pytest.raises(IbkrExecutionQuoteError, match="differs from the verified mapping"):
        _parse(_response(qualified_contract=contract))


@pytest.mark.parametrize(
    "observed_at",
    [
        (NOW - timedelta(seconds=11)).isoformat(),
        (NOW + timedelta(seconds=6)).isoformat(),
        "2026-11-02T15:00:00",
    ],
)
def test_quote_rejects_stale_future_or_naive_observation_timestamp(observed_at):
    with pytest.raises(IbkrExecutionQuoteError):
        _parse(_response(observed_at_utc=observed_at))


@pytest.mark.parametrize(
    "values",
    [
        {"bid": "NaN", "ask": "200"},
        {"bid": "0", "ask": "200"},
        {"bid": "201", "ask": "200"},
    ],
)
def test_quote_rejects_invalid_or_crossed_top_of_book(values):
    with pytest.raises(IbkrExecutionQuoteError):
        _parse(_response(quote=values))


def test_hkd_account_conversion_requires_fresh_verified_usdhkd_quote():
    quote = IbkrExecutionQuote(
        symbol="USD", con_id=1, security_type="CASH", exchange="IDEALPRO",
        primary_exchange="IDEALPRO", currency="HKD", bid=Decimal("7.80"),
        ask=Decimal("7.81"), observed_at=NOW,
    )
    assert convert_to_usd(Decimal("7810"), "HKD", usd_fx_quote=quote, now=NOW) == Decimal("1000")
    assert convert_to_usd(Decimal("100"), "USD", usd_fx_quote=None) == Decimal("100")
    with pytest.raises(IbkrExecutionQuoteError, match="fresh verified USD/CCY"):
        convert_to_usd(Decimal("100"), "HKD", usd_fx_quote=None)
    with pytest.raises(IbkrExecutionQuoteError, match="stale or from the future"):
        convert_to_usd(Decimal("100"), "HKD", usd_fx_quote=quote, now=NOW + timedelta(seconds=11))


def test_execution_price_rejects_invalid_side():
    quote = _parse(_response())
    with pytest.raises(ValueError, match="side must be"):
        quote.execution_price("SHORT")
