"""Single-intent, fail-closed reservation and dispatch state machine.

This service is an offline composition seam. It never connects to IBKR or
constructs SDK objects; callers inject an authoritative facts provider and a
send function. The atomic reservation check covers one shared SQLite ledger;
production still needs trusted external facts and order-to-intent binding.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from src.execution.contracts import Environment, RiskGrant
from src.execution.order_ledger import (
    ExecutionControlState,
    InvalidTransition,
    LedgerConflict,
    LocalOrderState,
    OrderIntentInput,
    SQLiteOrderLedger,
)
from src.execution.risk import ProposedOrder, RiskDecision, RiskFacts, evaluate_order_risk


@dataclass(frozen=True, slots=True)
class DispatchRiskSnapshot:
    grant: RiskGrant
    facts: RiskFacts
    deployment_id: str


class DispatchFactsProvider(Protocol):
    def __call__(self, intent: OrderIntentInput, *, excluding_intent_id: str) -> DispatchRiskSnapshot:
        """Return owner-bound facts and external holds, excluding local ledger holds."""


class DispatchSender(Protocol):
    def __call__(self, intent: OrderIntentInput, *, attempt_id: str, client_order_id: str) -> None:
        """Send exactly this intent after SEND_STARTED has committed."""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    intent_id: str
    status: str
    reason_codes: tuple[str, ...] = ()
    risk_snapshot_id: str | None = None


class DispatchServiceError(RuntimeError):
    """A dispatch request could not be bound to its durable order intent."""


class PaperOrderDispatchService:
    """Refresh risk, reserve, refresh again, then cross one injected send gate.

    The provider supplies external broker holds while excluding holds already
    present in this SQLite ledger. The ledger adds its own account/strategy
    reservations atomically with the final risk check and send-state commit.
    This does not coordinate independent ledgers or establish account ownership.
    """

    def __init__(
        self,
        *,
        ledger: SQLiteOrderLedger,
        facts_provider: DispatchFactsProvider,
        sender: DispatchSender,
        now: Callable[[], datetime],
    ) -> None:
        self.ledger = ledger
        self.facts_provider = facts_provider
        self.sender = sender
        self.now = now

    def dispatch(
        self,
        intent_id: str,
        *,
        sender: DispatchSender | None = None,
        broker_binding: dict | None = None,
    ) -> DispatchResult:
        intent = self.ledger.get_intent(intent_id)
        if intent.environment is not Environment.PAPER:
            return DispatchResult(intent_id, "REJECTED", ("paper_only",))
        control_state, _ = self.ledger.get_execution_control(
            broker=intent.broker,
            account_ref=intent.account_ref,
            environment=intent.environment,
        )
        if control_state is not ExecutionControlState.RUNNING:
            reason = (
                "execution_paused"
                if control_state is ExecutionControlState.PAUSED
                else "execution_not_running"
            )
            return DispatchResult(intent_id, "REJECTED", (reason,))
        record = self.ledger.get_order(intent_id)
        if record.state in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}:
            return DispatchResult(intent_id, "RECONCILE", ("send_outcome_unresolved",))
        if record.state not in {LocalOrderState.CREATED, LocalOrderState.RESERVED}:
            return DispatchResult(intent_id, record.state.value)

        reservation_id = f"dispatch:{intent_id}"
        if record.state is LocalOrderState.CREATED:
            first = self._check_current(intent, intent_id)
            if isinstance(first, DispatchResult):
                return first
            decision, checked_facts = first
            if intent.side == "SELL":
                try:
                    self.ledger.reserve_sell(
                        intent_id,
                        reservation_id,
                        available_position_qty=checked_facts.strategy_position_qty,
                        account_as_of=checked_facts.account_as_of,
                    )
                except LedgerConflict:
                    return DispatchResult(intent_id, "REJECTED", ("sell_position_reserved_or_changed",))
            else:
                self.ledger.reserve(intent_id, reservation_id, decision.estimated_cost)

        try:
            snapshot = self._load_current_snapshot(intent, intent_id)
        except Exception:
            # Risk data becoming unavailable before SEND_STARTED is a safe
            # rejection; release the hold so a later fresh check can retry.
            self.ledger.release_before_send(intent_id, reservation_id)
            raise
        if isinstance(snapshot, DispatchResult):
            self.ledger.release_before_send(intent_id, reservation_id)
            return snapshot

        # The identifiers are deterministic so crash recovery never invents a
        # second logical attempt. start_send is the durable no-retry boundary.
        suffix = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:32]
        attempt_id = f"attempt:{suffix}"
        client_order_id = f"client:{suffix}"
        try:
            _, second = self.ledger.start_send_after_risk(
                intent_id,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                grant=snapshot.grant,
                facts=snapshot.facts,
                deployment_id=snapshot.deployment_id,
                now=self.now,
                broker_binding=broker_binding,
            )
        except InvalidTransition:
            current = self.ledger.get_order(intent_id)
            if current.state in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}:
                return DispatchResult(intent_id, "RECONCILE", ("send_outcome_unresolved",))
            if current.state is LocalOrderState.RESERVED:
                self.ledger.release_before_send(intent_id, reservation_id)
            raise
        except Exception:
            # A failed/ambiguous SQLite call is resolved by reading committed
            # state. If SEND_STARTED did not commit, no sender was entered.
            current = self.ledger.get_order(intent_id)
            if current.state in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}:
                return DispatchResult(intent_id, "RECONCILE", ("send_outcome_unresolved",))
            if current.state is LocalOrderState.RESERVED:
                self.ledger.release_before_send(intent_id, reservation_id)
            raise
        if not second.allowed:
            return DispatchResult(intent_id, "REJECTED", second.reason_codes, second.risk_snapshot_id)

        try:
            (sender or self.sender)(intent, attempt_id=attempt_id, client_order_id=client_order_id)
        except Exception:
            self.ledger.mark_unknown(intent_id)
            raise
        return DispatchResult(intent_id, "SEND_STARTED", risk_snapshot_id=second.risk_snapshot_id)

    def _check_current(
        self, intent: OrderIntentInput, excluding_intent_id: str
    ) -> tuple[RiskDecision, RiskFacts] | DispatchResult:
        snapshot = self._load_current_snapshot(intent, excluding_intent_id)
        if isinstance(snapshot, DispatchResult):
            return snapshot
        grant, facts = snapshot.grant, snapshot.facts
        if intent.side == "SELL":
            if not self.ledger.sell_position_snapshot_is_current(
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
                instrument_id=intent.instrument_id,
                account_as_of=facts.account_as_of,
            ):
                return DispatchResult(
                    intent.intent_id, "REJECTED", ("position_snapshot_predates_sell_fill",),
                )
            held_shares = self.ledger.reserved_sell_quantity(
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
                instrument_id=intent.instrument_id,
                excluding_intent_id=excluding_intent_id,
            )
            facts = facts.model_copy(
                update={
                    "strategy_sell_reservations": facts.strategy_sell_reservations + held_shares,
                }
            )
        candidate = ProposedOrder(
            instrument_id=intent.instrument_id,
            side=intent.side,
            integer_qty=intent.integer_qty,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
        )
        decision = evaluate_order_risk(grant, candidate, facts)
        if not decision.allowed:
            return DispatchResult(
                excluding_intent_id,
                "REJECTED",
                decision.reason_codes,
                decision.risk_snapshot_id,
            )
        if decision.estimated_cost <= 0:
            raise DispatchServiceError("risk-approved dispatch has a non-positive cost estimate")
        return decision, facts

    def _load_current_snapshot(
        self,
        intent: OrderIntentInput,
        excluding_intent_id: str,
    ) -> DispatchRiskSnapshot | DispatchResult:
        snapshot = self.facts_provider(intent, excluding_intent_id=excluding_intent_id)
        grant, facts = snapshot.grant, snapshot.facts
        identity_reasons = []
        if snapshot.deployment_id != intent.deployment_id:
            identity_reasons.append("deployment_mismatch")
        if grant.broker != intent.broker or facts.broker != intent.broker:
            identity_reasons.append("broker_mismatch")
        if grant.account_ref != intent.account_ref or facts.account_ref != intent.account_ref:
            identity_reasons.append("account_mismatch")
        if grant.environment is not intent.environment or facts.environment is not intent.environment:
            identity_reasons.append("environment_mismatch")
        if grant.strategy_version_hash != intent.strategy_version_hash or facts.strategy_version_hash != intent.strategy_version_hash:
            identity_reasons.append("strategy_version_mismatch")
        if facts.instrument_id != intent.instrument_id:
            identity_reasons.append("instrument_mismatch")
        now = self.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DispatchServiceError("dispatch clock must be timezone-aware")
        now = now.astimezone(facts.now.tzinfo)
        if now < intent.eligible_at:
            identity_reasons.append("not_yet_eligible")
        if now >= intent.expires_at:
            identity_reasons.append("intent_expired")
        if facts.now > now:
            identity_reasons.append("risk_snapshot_from_future")
        if identity_reasons:
            return DispatchResult(
                excluding_intent_id,
                "REJECTED",
                tuple(sorted(set(identity_reasons))),
            )

        # Evaluate freshness and grant validity against the service clock,
        # rather than trusting a provider timestamp that may have gone stale
        # while the snapshot was being assembled.
        facts = facts.model_copy(update={"now": now})
        return DispatchRiskSnapshot(grant=grant, facts=facts, deployment_id=snapshot.deployment_id)
