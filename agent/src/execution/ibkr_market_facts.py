"""Strict IBKR quote facts for PAPER execution and currency conversion.

The read-only connector returns diagnostics as well as prices. This module
turns one successful response into a fresh, contract-bound quote only when the
Gateway explicitly reports live market data and a valid two-sided book.
Delayed/frozen data remains useful for research but is never promoted to an
execution fact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class IbkrExecutionQuoteError(ValueError):
    """An IBKR quote response cannot support execution risk checks."""


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IbkrExecutionQuoteError(f"{name} must be a finite positive decimal") from exc
    if not result.is_finite() or result <= 0:
        raise IbkrExecutionQuoteError(f"{name} must be a finite positive decimal")
    return result


class IbkrExecutionQuote:
    """One fresh live quote bound to an operator-pinned IBKR contract."""

    __slots__ = (
        "symbol", "con_id", "security_type", "exchange", "primary_exchange", "currency",
        "bid", "ask", "observed_at",
    )

    def __init__(
        self,
        *,
        symbol: str,
        con_id: int,
        security_type: str,
        exchange: str,
        primary_exchange: str,
        currency: str,
        bid: Decimal,
        ask: Decimal,
        observed_at: datetime,
    ) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise IbkrExecutionQuoteError("quote symbol is required")
        if type(con_id) is not int or con_id <= 0:
            raise IbkrExecutionQuoteError("quote contract ID must be a positive integer")
        if not all(isinstance(value, str) for value in (
            security_type, exchange, primary_exchange, currency,
        )) or not security_type or not exchange or not currency:
            raise IbkrExecutionQuoteError("quote contract identity is incomplete")
        bid = _decimal(bid, "bid")
        ask = _decimal(ask, "ask")
        if ask < bid:
            raise IbkrExecutionQuoteError("IBKR quote ask is below bid")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise IbkrExecutionQuoteError("quote observation timestamp must be timezone-aware")
        self.symbol = symbol.strip().upper()
        self.con_id = con_id
        self.security_type = security_type.strip().upper()
        self.exchange = exchange
        self.primary_exchange = primary_exchange
        self.currency = currency.strip().upper()
        self.bid = bid
        self.ask = ask
        self.observed_at = observed_at.astimezone(timezone.utc)

    @classmethod
    def from_connector_response(
        cls,
        response: Mapping[str, Any],
        *,
        expected_symbol: str,
        expected_con_id: int,
        expected_security_type: str,
        expected_exchange: str,
        expected_primary_exchange: str,
        expected_currency: str,
        now: datetime,
        max_age_seconds: int,
        max_future_skew_seconds: int = 5,
    ) -> "IbkrExecutionQuote":
        """Validate source tier, exact contract, spread, and receipt freshness."""
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise IbkrExecutionQuoteError("IBKR quote request did not succeed")
        if response.get("market_data_type_actual") != "live" or response.get("market_data_type_actual_code") != 1:
            raise IbkrExecutionQuoteError("IBKR quote is not confirmed live market data")
        if response.get("execution_ready") is not True or response.get("quote_quality") != "two_sided":
            raise IbkrExecutionQuoteError("IBKR quote does not contain an execution-ready two-sided market")
        if type(expected_con_id) is not int or expected_con_id <= 0:
            raise IbkrExecutionQuoteError("verified contract ID must be a positive integer")
        contract = response.get("qualified_contract")
        if not isinstance(contract, Mapping):
            raise IbkrExecutionQuoteError("IBKR quote has no qualified contract identity")
        expected = {
            "symbol": expected_symbol.upper(),
            "con_id": expected_con_id,
            "sec_type": expected_security_type.upper(),
            "exchange": expected_exchange,
            "primary_exchange": expected_primary_exchange,
            "currency": expected_currency.upper(),
        }
        actual = {
            "symbol": str(contract.get("symbol", "")).upper(),
            "con_id": contract.get("con_id"),
            "sec_type": str(contract.get("sec_type", "")).upper(),
            "exchange": contract.get("exchange"),
            "primary_exchange": contract.get("primary_exchange"),
            "currency": str(contract.get("currency", "")).upper(),
        }
        if actual != expected:
            raise IbkrExecutionQuoteError("IBKR quote contract differs from the verified mapping")
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise IbkrExecutionQuoteError("quote validation clock must be timezone-aware")
        if type(max_age_seconds) is not int or max_age_seconds <= 0:
            raise IbkrExecutionQuoteError("quote freshness limit must be a positive integer")
        if type(max_future_skew_seconds) is not int or max_future_skew_seconds < 0:
            raise IbkrExecutionQuoteError("future timestamp tolerance must be a non-negative integer")
        try:
            observed_at = datetime.fromisoformat(str(response["observed_at_utc"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise IbkrExecutionQuoteError("IBKR quote observation timestamp is invalid") from exc
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise IbkrExecutionQuoteError("IBKR quote observation timestamp must include a timezone")
        observed_at = observed_at.astimezone(timezone.utc)
        now = now.astimezone(timezone.utc)
        age = (now - observed_at).total_seconds()
        if age < -max_future_skew_seconds or age > max_age_seconds:
            raise IbkrExecutionQuoteError("IBKR quote observation is stale or from the future")
        quote = response.get("quote")
        if not isinstance(quote, Mapping):
            raise IbkrExecutionQuoteError("IBKR quote has no top-of-book values")
        bid = _decimal(quote.get("bid"), "bid")
        ask = _decimal(quote.get("ask"), "ask")
        if ask < bid:
            raise IbkrExecutionQuoteError("IBKR quote ask is below bid")
        return cls(
            symbol=expected["symbol"],
            con_id=expected_con_id,
            security_type=expected["sec_type"],
            exchange=expected_exchange,
            primary_exchange=expected_primary_exchange,
            currency=expected["currency"],
            bid=bid,
            ask=ask,
            observed_at=observed_at,
        )

    def execution_price(self, side: str) -> Decimal:
        """Return the conservative side of the observed top-of-book."""
        if side == "BUY":
            return self.ask
        if side == "SELL":
            return self.bid
        raise ValueError("side must be BUY or SELL")


def convert_to_usd(
    amount: Decimal,
    currency: str,
    *,
    usd_fx_quote: IbkrExecutionQuote | None,
    now: datetime | None = None,
    max_age_seconds: int = 10,
) -> Decimal:
    """Convert account/position currency into USD using USD/CCY ask.

    IBKR represents a CASH contract as base symbol USD and quote currency CCY.
    Dividing by its ask is conservative when valuing foreign-currency cash or
    exposure in USD. No inverse-rate guessing or stale/delayed fallback occurs.
    """
    amount = Decimal(str(amount))
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    currency = currency.upper()
    if currency == "USD":
        return amount
    if (
        usd_fx_quote is None
        or usd_fx_quote.security_type != "CASH"
        or usd_fx_quote.symbol != "USD"
        or usd_fx_quote.currency != currency
        or usd_fx_quote.exchange != "IDEALPRO"
    ):
        raise IbkrExecutionQuoteError("a fresh verified USD/CCY FX quote is required for conversion")
    if now is None or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise IbkrExecutionQuoteError("FX conversion requires an aware validation timestamp")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise IbkrExecutionQuoteError("FX freshness limit must be a positive integer")
    age = (now.astimezone(timezone.utc) - usd_fx_quote.observed_at).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise IbkrExecutionQuoteError("USD/CCY FX quote is stale or from the future")
    return amount / usd_fx_quote.ask


__all__ = ["IbkrExecutionQuote", "IbkrExecutionQuoteError", "convert_to_usd"]
