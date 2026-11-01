"""Build order risk facts from reconciled IBKR PAPER state and live quotes.

This adapter intentionally requires host supplied strategy P&L, session count,
and fee facts. It never treats missing values as zero and never accepts delayed
market data as an execution price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from src.execution.contracts import Environment, RiskGrant
from src.execution.ibkr_market_facts import (
    IbkrExecutionQuote,
    convert_to_usd,
)
from src.execution.risk import PositionFact, RiskFacts


class IbkrPaperRiskFactsError(ValueError):
    """Required account, quote, contract, or risk facts are not trustworthy."""


class IbkrRiskSnapshotSourceRebindError(ValueError):
    """A snapshot reader cannot move from one runtime epoch to another safely."""

    def __init__(self, blocked_components: list[str]) -> None:
        allowed = {
            "owner_lease", "sdk_process", "callback_worker", "health_monitor",
            "callback_queue", "pending_callbacks", "pending_inbox",
        }
        if not blocked_components or any(item not in allowed for item in blocked_components):
            raise ValueError("invalid risk snapshot rebind diagnostic")
        self.safe_error_code = "rebind_blocked_" + "_".join(blocked_components)
        super().__init__("risk snapshot source is still bound to an active runtime")


class IbkrLocalPaperQuoteReader:
    """Read live-tier stock and FX quotes through the local read-only connector.

    This adapter never changes the saved profile. It requests tier 1 for each
    quote so missing entitlements are visible as errors instead of silently
    falling back to delayed data; the downstream quote validator still
    requires an actual fresh live two-sided result.
    """

    def __init__(self, *, config: Any, account_ref: str) -> None:
        from src.trading.connectors.ibkr.local import IBKRLocalConfig

        if not isinstance(config, IBKRLocalConfig):
            raise TypeError("config must be IBKRLocalConfig")
        if (config.profile != "paper" or config.readonly is not True
                or config.port not in {4002, 7497}
                or config.host.strip().lower() not in {"localhost", "127.0.0.1", "::1"}
                or config.account != account_ref):
            raise ValueError("quote reader requires the exact loopback PAPER read-only account profile")
        self.config = config.with_overrides(market_data_type=1)

    def __call__(self, contract: VerifiedIbkrContract) -> Mapping[str, Any]:
        from src.trading.connectors.ibkr.local import get_quote

        if not isinstance(contract, VerifiedIbkrContract):
            raise TypeError("stock quote requires a VerifiedIbkrContract")
        return get_quote(
            contract.symbol,
            config=self.config,
            exchange=contract.exchange,
            currency=contract.currency,
            sec_type=contract.security_type,
        )

    def fx_quote(self, currency: str) -> Mapping[str, Any]:
        from src.trading.connectors.ibkr.local import get_quote

        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise ValueError("FX quote currency must be a three-letter ISO code")
        clean_currency = currency.upper()
        if clean_currency == "USD":
            raise ValueError("USD does not require an FX quote")
        return get_quote(
            "USD",
            config=self.config,
            exchange="IDEALPRO",
            currency=clean_currency,
            sec_type="CASH",
        )


@dataclass(frozen=True, slots=True)
class VerifiedIbkrContract:
    instrument_id: str
    symbol: str
    con_id: int
    security_type: str = "STK"
    exchange: str = "SMART"
    primary_exchange: str = "NASDAQ"
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.symbol or type(self.con_id) is not int or self.con_id <= 0:
            raise ValueError("verified instrument id, symbol, and positive contract id are required")
        if self.security_type != "STK" or self.exchange != "SMART" or self.currency != "USD":
            raise ValueError("V1 verified PAPER mappings support SMART USD stocks only")


@dataclass(frozen=True, slots=True)
class StrategyRiskMetrics:
    """Host-derived strategy state and external holds.

    Reservation values must exclude holds in ``SQLiteOrderLedger``; that
    ledger adds its own account/strategy/share reservations in the final send
    transaction. P&L and order count have no safe defaults.
    """

    strategy_position_qty: int
    strategy_sell_reservations: int
    strategy_buy_reservations: Decimal
    strategy_daily_loss: Decimal
    orders_this_session: int
    price_buffer_fraction: Decimal
    estimated_fees: Decimal


SnapshotReader = Callable[[], Mapping[str, Any]]
QuoteReader = Callable[[VerifiedIbkrContract], Mapping[str, Any]]
FxQuoteReader = Callable[[str], Mapping[str, Any]]
MetricsReader = Callable[[str, str, datetime], StrategyRiskMetrics]


class IbkrManagedRiskSnapshotSource:
    """Late-bind a runtime's fresh risk snapshot operation to a session.

    Session builders may bind this object while composing the unstarted
    runtime. Calls happen later, after startup/reconciliation, and request a
    new snapshot each time rather than reusing the startup hydration snapshot.
    """

    def __init__(self, *, account_ref: str) -> None:
        if not account_ref:
            raise ValueError("account_ref is required")
        self.account_ref = account_ref
        self._runtime = None

    def bind_runtime(self, runtime: Any) -> None:
        from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime

        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("risk snapshot source requires an IbkrManagedPaperRuntime")
        if (runtime.account_ref != self.account_ref or runtime.service.broker != "IBKR"
                or runtime.service.environment is not Environment.PAPER
                or runtime.service.account_ref != self.account_ref):
            raise ValueError("runtime scope differs from the risk snapshot source PAPER account")
        if self._runtime is not None and self._runtime is not runtime:
            previous = self._runtime
            blocked = []
            if previous.service.lease_held is not False:
                blocked.append("owner_lease")
            if previous.process.pid is not None and previous._process_closed is not True:
                blocked.append("sdk_process")
            if previous.callback_worker.alive:
                blocked.append("callback_worker")
            if previous.health_monitor.alive:
                blocked.append("health_monitor")
            if not previous.callback_pump.events.empty():
                blocked.append("callback_queue")
            if previous.order_index.pending_callbacks():
                blocked.append("pending_callbacks")
            if previous.order_index.pending_inbox():
                blocked.append("pending_inbox")
            if blocked:
                raise IbkrRiskSnapshotSourceRebindError(blocked)
        self._runtime = runtime

    def __call__(self) -> Mapping[str, Any]:
        if self._runtime is None:
            raise IbkrPaperRiskFactsError("risk snapshot source is not bound to the managed runtime")
        return self._runtime.refresh_risk_snapshot()


class IbkrPaperRiskFactsProvider:
    """Produce one exact intent's facts from fresh, reconciled PAPER evidence.

    The snapshot reader must return the managed runtime's captured and
    reconciled snapshot. ``metrics_reader`` must bind its result to the frozen
    strategy version and current session. Local ledger reservations are left
    at zero here because ``SQLiteOrderLedger`` adds them atomically at send.
    """

    def __init__(
        self,
        *,
        account_ref: str,
        strategy_version_hash: str,
        risk_grant: RiskGrant,
        contracts: Mapping[str, VerifiedIbkrContract],
        fx_contract_ids: Mapping[str, int],
        snapshot_reader: SnapshotReader,
        quote_reader: QuoteReader,
        fx_quote_reader: FxQuoteReader,
        metrics_reader: MetricsReader,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not account_ref or len(strategy_version_hash) != 64:
            raise ValueError("PAPER account and frozen strategy SHA-256 are required")
        if not isinstance(risk_grant, RiskGrant):
            raise TypeError("risk_grant must be a trusted RiskGrant")
        if (risk_grant.broker != "IBKR" or risk_grant.account_ref != account_ref
                or risk_grant.environment is not Environment.PAPER
                or risk_grant.strategy_version_hash != strategy_version_hash):
            raise ValueError("risk grant scope differs from configured PAPER strategy")
        if not contracts or any(key != value.instrument_id for key, value in contracts.items()):
            raise ValueError("contracts must be keyed by their exact instrument ids")
        if not set(risk_grant.allowed_instruments).issubset(contracts):
            raise ValueError("every grant instrument must have an operator verified contract mapping")
        for name, value in (("snapshot_reader", snapshot_reader), ("quote_reader", quote_reader),
                            ("fx_quote_reader", fx_quote_reader), ("metrics_reader", metrics_reader),
                            ("now", now)):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        if any(len([c for c in contracts.values() if c.con_id == cid]) != 1
               for cid in {c.con_id for c in contracts.values()}):
            raise ValueError("verified contract ids must be unique")
        self.account_ref = account_ref
        self.strategy_version_hash = strategy_version_hash
        self.risk_grant = risk_grant
        self.contracts = dict(contracts)
        self.by_con_id = {contract.con_id: contract for contract in contracts.values()}
        self.fx_contract_ids = dict(fx_contract_ids)
        if any(not isinstance(ccy, str) or len(ccy) != 3 or type(con_id) is not int or con_id <= 0
               for ccy, con_id in self.fx_contract_ids.items()):
            raise ValueError("FX contract mappings must pin currency and positive contract id")
        self.snapshot_reader = snapshot_reader
        self.quote_reader = quote_reader
        self.fx_quote_reader = fx_quote_reader
        self.metrics_reader = metrics_reader
        self.now = now

    def bind_runtime(self, runtime: Any) -> None:
        """Bind a late-bound snapshot source during session construction."""
        bind_runtime = getattr(self.snapshot_reader, "bind_runtime", None)
        if callable(bind_runtime):
            bind_runtime(runtime)

    def __call__(self, intent: Any, *, excluding_intent_id: str) -> Any:
        """Return ``DispatchRiskSnapshot`` without importing dispatch modules."""
        from src.execution.dispatch_service import DispatchRiskSnapshot

        facts = self.build(intent, self.risk_grant)
        return DispatchRiskSnapshot(
            grant=self.risk_grant,
            facts=facts,
            deployment_id=intent.deployment_id,
        )

    def build(self, intent: Any, grant: RiskGrant) -> RiskFacts:
        if grant != self.risk_grant:
            raise IbkrPaperRiskFactsError("requested grant differs from the installed trusted grant")
        now = self._aware(self.now(), "risk clock")
        if (intent.broker != "IBKR" or intent.account_ref != self.account_ref
                or intent.environment is not Environment.PAPER
                or intent.strategy_version_hash != self.strategy_version_hash):
            raise IbkrPaperRiskFactsError("intent scope differs from configured PAPER strategy")
        try:
            contract = self.contracts[intent.instrument_id]
        except KeyError as exc:
            raise IbkrPaperRiskFactsError("intent instrument has no operator verified contract mapping") from exc

        snapshot = self.snapshot_reader()
        completed = self._aware(snapshot.get("completed_at_utc"), "snapshot completion time")
        if (snapshot.get("capture_complete") is not True or snapshot.get("reconciled") is not True
                or snapshot.get("account_ref") != self.account_ref or snapshot.get("environment") != "paper"):
            raise IbkrPaperRiskFactsError("account snapshot is not a complete reconciled PAPER snapshot")
        if not snapshot.get("facts_sha256"):
            raise IbkrPaperRiskFactsError("reconciled snapshot has no integrity digest")
        self._rows(snapshot.get("open_orders"), "open orders")
        account_age = (now - completed).total_seconds()
        if account_age < 0 or account_age > grant.account_max_age_seconds:
            raise IbkrPaperRiskFactsError("reconciled account snapshot is stale or from the future")

        account_values = self._account_values(snapshot.get("account_summary"))
        account_currency = account_values["currency"]
        fx = self._fx_quote(account_currency, now, grant.quote_max_age_seconds) if account_currency != "USD" else None
        net_liquidation = convert_to_usd(account_values["NetLiquidation"], account_currency,
                                         usd_fx_quote=fx, now=now,
                                         max_age_seconds=grant.quote_max_age_seconds)
        available_cash = convert_to_usd(account_values["AvailableFunds"], account_currency,
                                        usd_fx_quote=fx, now=now,
                                        max_age_seconds=grant.quote_max_age_seconds)

        target_quote = self._quote(contract, now, grant.quote_max_age_seconds)
        target_price = target_quote.execution_price(intent.side)
        positions: list[PositionFact] = []
        seen: set[int] = set()
        for row in self._rows(snapshot.get("positions"), "positions"):
            con_id = row.get("contract_id")
            if type(con_id) is not int or con_id in seen:
                raise IbkrPaperRiskFactsError("position snapshot contains a missing or duplicate contract id")
            seen.add(con_id)
            mapped = self.by_con_id.get(con_id)
            if mapped is None or row.get("symbol") != mapped.symbol or row.get("currency") != mapped.currency:
                raise IbkrPaperRiskFactsError("account position has no exact verified contract mapping")
            quantity = self._decimal(row.get("quantity"), "position quantity")
            if quantity != quantity.to_integral_value() or quantity < 0:
                raise IbkrPaperRiskFactsError("position quantity must be a non-negative whole number")
            quote = target_quote if con_id == contract.con_id else self._quote(
                mapped, now, grant.quote_max_age_seconds,
            )
            mark = quote.bid if quantity > 0 else quote.ask
            usd_mark = convert_to_usd(mark, mapped.currency,
                                      usd_fx_quote=fx if mapped.currency == account_currency else None,
                                      now=now, max_age_seconds=grant.quote_max_age_seconds)
            positions.append(PositionFact(
                instrument_id=mapped.instrument_id,
                quantity=int(quantity),
                market_price=usd_mark,
            ))

        metrics = self.metrics_reader(self.strategy_version_hash, intent.instrument_id, now)
        if not isinstance(metrics, StrategyRiskMetrics):
            raise IbkrPaperRiskFactsError("host did not provide complete strategy risk/cost facts")
        account_position_qty = next((pos.quantity for pos in positions
                                     if pos.instrument_id == intent.instrument_id), 0)
        if metrics.strategy_position_qty > account_position_qty:
            raise IbkrPaperRiskFactsError("strategy-owned quantity exceeds reconciled account position")
        return RiskFacts(
            broker="IBKR",
            account_ref=self.account_ref,
            environment=Environment.PAPER,
            strategy_version_hash=self.strategy_version_hash,
            instrument_id=intent.instrument_id,
            now=now,
            quote_as_of=target_quote.observed_at,
            account_as_of=completed,
            quote_price=target_price,
            price_buffer_fraction=metrics.price_buffer_fraction,
            estimated_fees=metrics.estimated_fees,
            net_liquidation=net_liquidation,
            available_cash=available_cash,
            positions=tuple(positions),
            strategy_position_qty=metrics.strategy_position_qty,
            strategy_sell_reservations=metrics.strategy_sell_reservations,
            strategy_buy_reservations=metrics.strategy_buy_reservations,
            account_buy_reservations=Decimal("0"),
            strategy_daily_loss=metrics.strategy_daily_loss,
            orders_this_session=metrics.orders_this_session,
            account_snapshot_complete=True,
        )

    def _quote(self, contract: VerifiedIbkrContract, now: datetime, max_age: int) -> IbkrExecutionQuote:
        try:
            return IbkrExecutionQuote.from_connector_response(
                self.quote_reader(contract),
                expected_symbol=contract.symbol,
                expected_con_id=contract.con_id,
                expected_security_type=contract.security_type,
                expected_exchange=contract.exchange,
                expected_primary_exchange=contract.primary_exchange,
                expected_currency=contract.currency,
                now=now,
                max_age_seconds=max_age,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise IbkrPaperRiskFactsError(f"no fresh live two-sided quote for {contract.symbol}") from exc

    def _fx_quote(self, currency: str, now: datetime, max_age: int) -> IbkrExecutionQuote:
        if not currency or currency == "USD":
            raise IbkrPaperRiskFactsError("FX conversion currency is invalid")
        response = self.fx_quote_reader(currency)
        con_id = self.fx_contract_ids.get(currency)
        if type(con_id) is not int:
            raise IbkrPaperRiskFactsError(f"USD/{currency} has no operator verified FX contract mapping")
        try:
            return IbkrExecutionQuote.from_connector_response(
                response,
                expected_symbol="USD",
                expected_con_id=con_id,
                expected_security_type="CASH",
                expected_exchange="IDEALPRO",
                expected_primary_exchange="",
                expected_currency=currency,
                now=now,
                max_age_seconds=max_age,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise IbkrPaperRiskFactsError(f"no fresh verified USD/{currency} FX quote") from exc

    @staticmethod
    def _account_values(rows: Any) -> dict[str, Decimal | str]:
        values = IbkrPaperRiskFactsProvider._rows(rows, "account summary")
        required = {"NetLiquidation", "TotalCashValue", "AvailableFunds"}
        selected = [row for row in values if row.get("tag") in required]
        currencies = {row.get("currency") for row in selected}
        tags = [row.get("tag") for row in selected]
        if len(currencies) != 1 or set(tags) != required or len(tags) != len(required):
            raise IbkrPaperRiskFactsError("account summary must contain each required tag once in one currency")
        result: dict[str, Decimal | str] = {"currency": next(iter(currencies))}
        for row in selected:
            result[str(row["tag"])] = IbkrPaperRiskFactsProvider._decimal(row.get("value"), str(row["tag"]))
        if result["NetLiquidation"] <= 0 or result["AvailableFunds"] < 0:
            raise IbkrPaperRiskFactsError("account summary contains invalid net liquidation or available funds")
        return result

    @staticmethod
    def _rows(value: Any, label: str) -> list[Mapping[str, Any]]:
        if not isinstance(value, (list, tuple)) or any(not isinstance(row, Mapping) for row in value):
            raise IbkrPaperRiskFactsError(f"{label} must be a complete list of records")
        return list(value)

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise IbkrPaperRiskFactsError(f"{label} must be a finite decimal") from exc
        if not result.is_finite():
            raise IbkrPaperRiskFactsError(f"{label} must be a finite decimal")
        return result

    @staticmethod
    def _aware(value: Any, label: str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise IbkrPaperRiskFactsError(f"{label} is invalid") from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise IbkrPaperRiskFactsError(f"{label} must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "IbkrLocalPaperQuoteReader",
    "IbkrManagedRiskSnapshotSource",
    "IbkrPaperRiskFactsError",
    "IbkrPaperRiskFactsProvider",
    "StrategyRiskMetrics",
    "VerifiedIbkrContract",
]
