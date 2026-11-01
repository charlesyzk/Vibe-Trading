"""Fresh, narrowly scoped account facts for the fixed PAPER integration order.

This provider does not invent an executable quote or convert HKD buying power
to USD. The explicit $400 LMT cap bounds the test position; IBKR remains the
authority that accepts or rejects USD buying power. It is not a general
strategy risk provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from src.execution.contracts import Environment, RiskGrant, is_sha256
from src.execution.dispatch_service import DispatchRiskSnapshot
from src.execution.outbox_dispatch import ProposalRiskInputs
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.paper_integration_test_config import PaperIntegrationTestConfig
from src.execution.risk import RiskFacts


class PaperIntegrationTestRiskFactsError(ValueError):
    """The exact one-shot test cannot produce current trusted account facts."""


class PaperIntegrationTestRiskFactsProvider:
    """Create exact AAPL test grant/facts from a fresh reconciled PAPER snapshot.

    ``snapshot_reader`` must be backed by ``IbkrManagedRiskSnapshotSource`` or
    an equivalent trusted host adapter that obtains a new reconciled snapshot
    per call. The runtime binding is late because the builder runs before the
    managed SDK process starts.
    """

    def __init__(
        self,
        *,
        config: PaperIntegrationTestConfig,
        strategy_version_hash: str,
        deployment_id: str,
        snapshot_reader: Callable[[], Mapping[str, Any]],
        ledger: SQLiteOrderLedger | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(config, PaperIntegrationTestConfig):
            raise TypeError("config must be a validated PaperIntegrationTestConfig")
        if config.strategy_version_id is not None and config.strategy_version_id != strategy_version_hash:
            raise ValueError("integration-test strategy hash differs from its pinned configuration")
        if not is_sha256(strategy_version_hash):
            raise ValueError("integration-test strategy version must be a SHA-256 hash")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id is required")
        if not callable(snapshot_reader) or not callable(now):
            raise TypeError("snapshot_reader and now must be callable")
        if ledger is not None and not isinstance(ledger, SQLiteOrderLedger):
            raise TypeError("ledger must be a SQLiteOrderLedger or None")
        self.config = config
        self.strategy_version_hash = strategy_version_hash
        self.deployment_id = deployment_id.strip()
        self.snapshot_reader = snapshot_reader
        self.ledger = ledger
        self.now = now

    def bind_runtime(self, runtime: Any) -> None:
        """Bind the provider to the exact unstarted managed PAPER runtime."""
        from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime

        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("runtime must be an IbkrManagedPaperRuntime")
        if (
            runtime.account_ref != self.config.account_ref
            or runtime.service.broker != "IBKR"
            or runtime.service.environment is not Environment.PAPER
            or runtime.service.account_ref != self.config.account_ref
        ):
            raise ValueError("runtime scope differs from the fixed integration-test PAPER account")
        bind_reader = getattr(self.snapshot_reader, "bind_runtime", None)
        if not callable(bind_reader):
            raise TypeError("snapshot_reader must support trusted managed-runtime binding")
        if self.ledger is not None:
            try:
                ledger_path = Path(self.ledger.path).expanduser().resolve()
                runtime_ledger_path = Path(runtime.ledger.path).expanduser().resolve()
            except Exception as exc:
                raise PaperIntegrationTestRiskFactsError(
                    "provider ledger path cannot be verified"
                ) from exc
            if ledger_path != runtime_ledger_path:
                raise PaperIntegrationTestRiskFactsError(
                    "provider ledger differs from the managed runtime database"
                )
        bind_reader(runtime)
        self.ledger = runtime.ledger

    def __call__(self, intent: Any, *, excluding_intent_id: str) -> DispatchRiskSnapshot:
        now = self._aware(self.now(), "risk clock")
        config = self.config
        if not config.is_submission_window_open(now):
            raise PaperIntegrationTestRiskFactsError("integration-test order is disabled or outside its valid window")
        self._validate_intent(intent)
        if self.ledger is None:
            raise PaperIntegrationTestRiskFactsError("provider is not bound to the managed execution ledger")

        try:
            snapshot = self.snapshot_reader()
        except Exception as exc:
            raise PaperIntegrationTestRiskFactsError("fresh PAPER risk snapshot failed") from exc
        completed = self._aware(snapshot.get("completed_at_utc"), "snapshot completion time")
        if (
            snapshot.get("capture_complete") is not True
            or snapshot.get("reconciled") is not True
            or snapshot.get("account_ref") != config.account_ref
            or snapshot.get("environment") != Environment.PAPER.value
            or not is_sha256(snapshot.get("facts_sha256"))
        ):
            raise PaperIntegrationTestRiskFactsError("risk snapshot is not complete, reconciled, and scope-bound")
        age = (now - completed).total_seconds()
        if age < 0 or age > config.risk.account_max_age_seconds:
            raise PaperIntegrationTestRiskFactsError("reconciled PAPER account snapshot is stale or from the future")

        self._validate_account_summary(snapshot.get("account_summary"))
        positions = self._rows(snapshot.get("positions"), "positions")
        open_orders = self._rows(snapshot.get("open_orders"), "open orders")
        if positions or open_orders:
            raise PaperIntegrationTestRiskFactsError("one-shot AAPL test requires zero account positions and open orders")
        self._validate_ledger(excluding_intent_id, intent)

        risk_grant = RiskGrant(
            grant_id=f"paper-integration-test:{self.strategy_version_hash}",
            user_action_id=f"paper-integration-test:{self.strategy_version_hash}",
            strategy_version_hash=self.strategy_version_hash,
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            allowed_instruments=(config.instrument.symbol,),
            allowed_order_types=("limit",),
            long_only=True,
            whole_shares=True,
            max_order_notional=config.risk.max_order_notional_usd,
            max_gross_exposure=config.risk.max_gross_exposure_usd,
            strategy_budget=config.risk.strategy_budget_usd,
            max_daily_loss=config.risk.max_daily_loss_usd,
            max_orders_per_session=config.risk.max_orders_per_session,
            valid_from=config.valid_window_utc.start,
            valid_until=config.valid_window_utc.end,
            quote_max_age_seconds=config.risk.quote_max_age_seconds,
            account_max_age_seconds=config.risk.account_max_age_seconds,
            funds_check=config.risk.funds_check,
            halt_action="pause_and_cancel_owned",
            liquidation_policy="none",
            granted_at=now,
        )
        risk_facts = RiskFacts(
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            strategy_version_hash=self.strategy_version_hash,
            instrument_id=config.instrument.symbol,
            now=now,
            quote_as_of=None,
            account_as_of=completed,
            quote_price=None,
            price_buffer_fraction=config.risk.price_buffer_fraction,
            estimated_fees=config.risk.estimated_fee_reserve_usd,
            net_liquidation=None,
            available_cash=None,
            positions=(),
            strategy_position_qty=0,
            strategy_sell_reservations=0,
            strategy_buy_reservations=Decimal("0"),
            account_buy_reservations=Decimal("0"),
            strategy_daily_loss=Decimal("0"),
            orders_this_session=0,
            account_snapshot_complete=True,
        )
        return DispatchRiskSnapshot(
            grant=risk_grant,
            facts=risk_facts,
            deployment_id=self.deployment_id,
        )

    def proposal_inputs(self, proposal: Any) -> ProposalRiskInputs:
        """Authorize the pre-ledger proposal from the same fresh account evidence."""
        now = self._aware(self.now(), "risk clock")
        config = self.config
        if not config.is_submission_window_open(now):
            raise PaperIntegrationTestRiskFactsError("integration-test order is disabled or outside its valid window")
        order = config.order
        raw_proposal = getattr(proposal, "proposal", None)
        try:
            proposed_limit = Decimal(str(raw_proposal.get("limit_price")))
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            proposed_limit = None
        if (
            getattr(proposal, "bar", None) is None
            or getattr(proposal.bar, "instrument_id", None) != config.instrument.symbol
            or not isinstance(raw_proposal, Mapping)
            or raw_proposal.get("side") != order.side
            or raw_proposal.get("integer_qty") != order.quantity
            or raw_proposal.get("order_type") != "limit"
            or raw_proposal.get("time_in_force") != order.time_in_force
            or proposed_limit != order.limit_price
        ):
            raise PaperIntegrationTestRiskFactsError("proposal differs from the exact one-shot PAPER order")
        if self.ledger is None:
            raise PaperIntegrationTestRiskFactsError("provider is not bound to the managed execution ledger")
        try:
            snapshot = self.snapshot_reader()
        except Exception as exc:
            raise PaperIntegrationTestRiskFactsError("fresh PAPER risk snapshot failed") from exc
        completed = self._validate_snapshot(snapshot, now)
        self._validate_account_summary(snapshot.get("account_summary"))
        positions = self._rows(snapshot.get("positions"), "positions")
        open_orders = self._rows(snapshot.get("open_orders"), "open orders")
        if positions or open_orders:
            raise PaperIntegrationTestRiskFactsError("one-shot AAPL test requires zero account positions and open orders")
        recovery = self.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=config.account_ref, environment=Environment.PAPER,
        )
        if any((recovery.ready_to_reserve, recovery.ready_to_send, recovery.unresolved_sends, recovery.active_orders)):
            raise PaperIntegrationTestRiskFactsError("another local order is active or unresolved")
        if self.ledger.has_strategy_version_intent(
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            strategy_version_hash=self.strategy_version_hash,
        ):
            raise PaperIntegrationTestRiskFactsError("the one-shot test strategy already has an order in its ledger")

        grant = self._risk_grant(now)
        facts = self._risk_facts(now, completed)
        return ProposalRiskInputs(
            grant=grant,
            facts=facts,
            eligible_at=now,
            expires_at=grant.valid_until,
        )

    def _validate_snapshot(self, snapshot: Mapping[str, Any], now: datetime) -> datetime:
        config = self.config
        completed = self._aware(snapshot.get("completed_at_utc"), "snapshot completion time")
        if (
            snapshot.get("capture_complete") is not True
            or snapshot.get("reconciled") is not True
            or snapshot.get("account_ref") != config.account_ref
            or snapshot.get("environment") != Environment.PAPER.value
            or not is_sha256(snapshot.get("facts_sha256"))
        ):
            raise PaperIntegrationTestRiskFactsError("risk snapshot is not complete, reconciled, and scope-bound")
        age = (now - completed).total_seconds()
        if age < 0 or age > config.risk.account_max_age_seconds:
            raise PaperIntegrationTestRiskFactsError("reconciled PAPER account snapshot is stale or from the future")
        return completed

    def _risk_grant(self, now: datetime) -> RiskGrant:
        config = self.config
        return RiskGrant(
            grant_id=f"paper-integration-test:{self.strategy_version_hash}",
            user_action_id=f"paper-integration-test:{self.strategy_version_hash}",
            strategy_version_hash=self.strategy_version_hash,
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            allowed_instruments=(config.instrument.symbol,),
            allowed_order_types=("limit",),
            long_only=True,
            whole_shares=True,
            max_order_notional=config.risk.max_order_notional_usd,
            max_gross_exposure=config.risk.max_gross_exposure_usd,
            strategy_budget=config.risk.strategy_budget_usd,
            max_daily_loss=config.risk.max_daily_loss_usd,
            max_orders_per_session=config.risk.max_orders_per_session,
            valid_from=config.valid_window_utc.start,
            valid_until=config.valid_window_utc.end,
            quote_max_age_seconds=config.risk.quote_max_age_seconds,
            account_max_age_seconds=config.risk.account_max_age_seconds,
            funds_check=config.risk.funds_check,
            halt_action="pause_and_cancel_owned",
            liquidation_policy="none",
            granted_at=now,
        )

    def _risk_facts(self, now: datetime, completed: datetime) -> RiskFacts:
        config = self.config
        return RiskFacts(
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            strategy_version_hash=self.strategy_version_hash,
            instrument_id=config.instrument.symbol,
            now=now,
            quote_as_of=None,
            account_as_of=completed,
            quote_price=None,
            price_buffer_fraction=config.risk.price_buffer_fraction,
            estimated_fees=config.risk.estimated_fee_reserve_usd,
            net_liquidation=None,
            available_cash=None,
            positions=(),
            strategy_position_qty=0,
            strategy_sell_reservations=0,
            strategy_buy_reservations=Decimal("0"),
            account_buy_reservations=Decimal("0"),
            strategy_daily_loss=Decimal("0"),
            orders_this_session=0,
            account_snapshot_complete=True,
        )

    def _validate_intent(self, intent: Any) -> None:
        config = self.config
        exact = {
            "broker": "IBKR",
            "account_ref": config.account_ref,
            "environment": Environment.PAPER,
            "deployment_id": self.deployment_id,
            "strategy_version_hash": self.strategy_version_hash,
            "instrument_id": config.instrument.symbol,
            "side": config.order.side,
            "integer_qty": config.order.quantity,
            "order_type": "limit",
            "time_in_force": config.order.time_in_force,
            "limit_price": config.order.limit_price,
        }
        if any(getattr(intent, key, None) != value for key, value in exact.items()):
            raise PaperIntegrationTestRiskFactsError("intent differs from the exact one-shot PAPER order")

    def _validate_ledger(self, excluding_intent_id: str, intent: Any) -> None:
        assert self.ledger is not None
        config = self.config
        try:
            persisted_intent = self.ledger.get_intent(excluding_intent_id)
        except Exception as exc:
            raise PaperIntegrationTestRiskFactsError("current order is not present in the trusted ledger") from exc
        if persisted_intent != intent:
            raise PaperIntegrationTestRiskFactsError("current order differs from its trusted ledger record")
        recovery = self.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=config.account_ref, environment=Environment.PAPER,
        )
        buckets = (
            recovery.ready_to_reserve,
            recovery.ready_to_send,
            recovery.unresolved_sends,
            recovery.active_orders,
        )
        for bucket in buckets:
            if any(record.intent_id != excluding_intent_id for record in bucket):
                raise PaperIntegrationTestRiskFactsError("another local order is active or unresolved")
        if self.ledger.has_strategy_version_intent(
            broker="IBKR",
            account_ref=config.account_ref,
            environment=Environment.PAPER,
            strategy_version_hash=self.strategy_version_hash,
            excluding_intent_id=excluding_intent_id,
        ):
            raise PaperIntegrationTestRiskFactsError("the one-shot test strategy already has an order in its ledger")

    @classmethod
    def _validate_account_summary(cls, rows: Any) -> None:
        values = cls._rows(rows, "account summary")
        required = {"NetLiquidation", "TotalCashValue", "AvailableFunds"}
        selected = [row for row in values if row.get("tag") in required]
        tags = [row.get("tag") for row in selected]
        currencies = {row.get("currency") for row in selected}
        if set(tags) != required or len(tags) != len(required) or len(currencies) != 1:
            raise PaperIntegrationTestRiskFactsError("required account summary tags are incomplete or currency-mixed")
        for row in selected:
            value = cls._decimal(row.get("value"), str(row.get("tag")))
            if row["tag"] == "NetLiquidation" and value <= 0:
                raise PaperIntegrationTestRiskFactsError("account NetLiquidation is not positive")
            if row["tag"] == "AvailableFunds" and value < 0:
                raise PaperIntegrationTestRiskFactsError("account AvailableFunds is negative")

    @staticmethod
    def _rows(value: Any, label: str) -> list[Mapping[str, Any]]:
        if not isinstance(value, (list, tuple)) or any(not isinstance(row, Mapping) for row in value):
            raise PaperIntegrationTestRiskFactsError(f"{label} are incomplete")
        return list(value)

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PaperIntegrationTestRiskFactsError(f"{label} is invalid") from exc
        if not result.is_finite():
            raise PaperIntegrationTestRiskFactsError(f"{label} is invalid")
        return result

    @staticmethod
    def _aware(value: Any, label: str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise PaperIntegrationTestRiskFactsError(f"{label} is invalid") from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PaperIntegrationTestRiskFactsError(f"{label} must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = ["PaperIntegrationTestRiskFactsError", "PaperIntegrationTestRiskFactsProvider"]
