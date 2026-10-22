"""Construct official IB API stock contracts/orders from trusted host data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ibapi.contract import Contract
from ibapi.order import Order


@dataclass(frozen=True, slots=True)
class VerifiedStockContract:
    instrument_id: str
    con_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    verified_at: datetime
    verification_source: str
    security_type: str = "STK"

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.symbol or not self.primary_exchange:
            raise ValueError("verified contract requires instrument, symbol, and primary exchange")
        if type(self.con_id) is not int or self.con_id <= 0:
            raise ValueError("verified contract conId must be a positive integer")
        if self.security_type != "STK" or self.currency != "USD" or self.exchange != "SMART":
            raise ValueError("V1 contract map supports SMART-routed USD STK contracts only")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("contract verification timestamp must include a timezone")
        if not self.verification_source.strip():
            raise ValueError("contract verification source is required")
        object.__setattr__(self, "verified_at", self.verified_at.astimezone(timezone.utc))


class IbapiOrderFactory:
    """Build exact MKT/DAY SDK objects after intent and contract-map checks."""

    def __init__(
        self,
        *,
        contracts: Mapping[str, VerifiedStockContract],
        account_ref: str,
        max_contract_age: timedelta = timedelta(days=30),
    ) -> None:
        if not account_ref:
            raise ValueError("a configured broker account is required")
        if max_contract_age <= timedelta(0):
            raise ValueError("max_contract_age must be positive")
        self.contracts = dict(contracts)
        self.account_ref = account_ref
        self.max_contract_age = max_contract_age

    def build(self, intent: Any, *, now: datetime | None = None) -> tuple[Contract, Order]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("order factory clock must be timezone-aware")
        now = now.astimezone(timezone.utc)
        if getattr(intent, "broker", None) != "IBKR":
            raise ValueError("intent broker is not IBKR")
        environment = getattr(getattr(intent, "environment", None), "value", getattr(intent, "environment", None))
        if environment != "paper":
            raise ValueError("official order factory is paper-only")
        if getattr(intent, "account_ref", None) != self.account_ref:
            raise ValueError("intent account differs from configured paper account")
        if getattr(intent, "order_type", None) != "market" or getattr(intent, "time_in_force", None) != "DAY":
            raise ValueError("V1 supports Market/DAY orders only")
        side = getattr(intent, "side", None)
        quantity = getattr(intent, "integer_qty", None)
        if side not in {"BUY", "SELL"}:
            raise ValueError("order side must be BUY or SELL")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("order quantity must be a positive whole-share integer")

        instrument_id = getattr(intent, "instrument_id", None)
        mapping = self.contracts.get(instrument_id)
        if mapping is None or mapping.instrument_id != instrument_id:
            raise ValueError("intent has no verified IBKR contract mapping")
        age = now - mapping.verified_at
        if age < timedelta(0) or age > self.max_contract_age:
            raise ValueError("IBKR contract mapping is stale or from the future")

        contract = Contract()
        contract.conId = mapping.con_id
        contract.symbol = mapping.symbol
        contract.secType = mapping.security_type
        contract.exchange = mapping.exchange
        contract.primaryExchange = mapping.primary_exchange
        contract.currency = mapping.currency

        order = Order()
        order.action = side
        order.totalQuantity = quantity
        order.orderType = "MKT"
        order.tif = "DAY"
        order.account = self.account_ref
        order.outsideRth = False
        order.transmit = True
        return contract, order
