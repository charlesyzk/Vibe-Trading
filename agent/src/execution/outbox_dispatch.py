"""Fail-closed handoff from strategy proposals to durable order intents.

This module has no broker or network calls. A trusted host supplied
``authorize`` callback must resolve the frozen instrument and provide the
current grant and account/quote facts. Approved intents are frozen in the
event journal before they are copied to the separate order ledger, making the
two-store handoff replayable after a crash. Reservation and a second risk
check remain mandatory before any future send boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol

from src.execution.contracts import Bar, Environment, RiskGrant, canonical_sha256
from src.execution.event_journal import SQLiteEventJournal
from src.execution.order_ledger import OrderIntentInput, OrderRecord, SQLiteOrderLedger
from src.execution.risk import ProposedOrder, RiskDecision, RiskFacts, evaluate_order_risk


@dataclass(frozen=True, slots=True)
class OutboxProposal:
    run_id: str
    event_id: str
    seq: int
    proposal_index: int
    proposal: dict
    identity: dict[str, str]
    bar: Bar


@dataclass(frozen=True, slots=True)
class AuthorizedProposal:
    """Trusted host decision; all authority fields must match journal facts."""

    intent: OrderIntentInput | None
    decision: RiskDecision
    broker: str
    reference_price: Decimal


class ProposalAuthorizer(Protocol):
    def __call__(self, proposal: OutboxProposal) -> AuthorizedProposal:
        """Resolve a grant, instrument, quote and account facts on the host."""


@dataclass(frozen=True, slots=True)
class ProposalRiskInputs:
    grant: RiskGrant
    facts: RiskFacts
    eligible_at: datetime
    expires_at: datetime


class ProposalRiskInputsProvider(Protocol):
    def __call__(self, proposal: OutboxProposal) -> ProposalRiskInputs:
        """Load current host-owned grant, account/quote facts and session window."""


class RiskAwareProposalAuthorizer:
    """Bind a worker proposal to host risk facts and create an intent if allowed.

    ``risk_inputs`` must be backed by the execution service's authoritative
    grant and account/quote providers. This class performs the actual risk
    evaluation and derives all intent identity fields from host records.
    """

    def __init__(self, risk_inputs: ProposalRiskInputsProvider) -> None:
        self.risk_inputs = risk_inputs

    def __call__(self, proposal: OutboxProposal) -> AuthorizedProposal:
        inputs = self.risk_inputs(proposal)
        candidate = ProposedOrder(
            instrument_id=proposal.bar.instrument_id,
            side=proposal.proposal["side"],
            integer_qty=proposal.proposal["integer_qty"],
            order_type=proposal.proposal["order_type"],
            time_in_force=proposal.proposal["time_in_force"],
        )
        decision = evaluate_order_risk(inputs.grant, candidate, inputs.facts)
        identity = proposal.identity
        identity_reasons = []
        if inputs.grant.account_ref != identity["account_ref"]:
            identity_reasons.append("grant_account_mismatch")
        if inputs.grant.environment.value != identity["environment"]:
            identity_reasons.append("grant_environment_mismatch")
        if inputs.grant.strategy_version_hash != identity["strategy_version_hash"]:
            identity_reasons.append("grant_strategy_version_mismatch")
        if inputs.facts.account_ref != identity["account_ref"]:
            identity_reasons.append("risk_facts_account_mismatch")
        if inputs.facts.environment.value != identity["environment"]:
            identity_reasons.append("risk_facts_environment_mismatch")
        if inputs.facts.strategy_version_hash != identity["strategy_version_hash"]:
            identity_reasons.append("risk_facts_strategy_version_mismatch")
        if identity_reasons:
            reasons = tuple(sorted(set(decision.reason_codes).union(identity_reasons)))
            decision = RiskDecision(
                allowed=False,
                reason_codes=reasons,
                checked_at=decision.checked_at,
                risk_snapshot_id=canonical_sha256({
                    "base_risk_snapshot_id": decision.risk_snapshot_id,
                    "run_identity": identity,
                    "identity_mismatch_reasons": reasons,
                }),
                estimated_notional=decision.estimated_notional,
                estimated_cost=decision.estimated_cost,
                projected_strategy_exposure=decision.projected_strategy_exposure,
                projected_gross_exposure=decision.projected_gross_exposure,
            )
        if not decision.allowed:
            return AuthorizedProposal(
                intent=None,
                decision=decision,
                broker=inputs.grant.broker,
                reference_price=inputs.facts.quote_price,
            )

        eligible_at = inputs.eligible_at
        expires_at = inputs.expires_at
        if eligible_at.tzinfo is None or eligible_at.utcoffset() is None:
            raise OutboxDispatchError("dispatch eligibility must be timezone-aware")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise OutboxDispatchError("dispatch expiry must be timezone-aware")
        if eligible_at < inputs.facts.now or expires_at > inputs.grant.valid_until:
            raise OutboxDispatchError("dispatch window is stale or exceeds the risk grant")
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker=inputs.grant.broker,
            account_ref=identity["account_ref"],
            environment=Environment(identity["environment"]),
            deployment_id=identity["deployment_id"],
            strategy_version_hash=identity["strategy_version_hash"],
            input_event_seq=proposal.seq,
            action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at,
            instrument_id=proposal.bar.instrument_id,
            side=candidate.side,
            integer_qty=candidate.integer_qty,
            order_type=candidate.order_type,
            time_in_force=candidate.time_in_force,
            reference_price=inputs.facts.quote_price,
            eligible_at=eligible_at,
            expires_at=expires_at,
            risk_snapshot_id=decision.risk_snapshot_id,
        )
        return AuthorizedProposal(
            intent=intent,
            decision=decision,
            broker=inputs.grant.broker,
            reference_price=inputs.facts.quote_price,
        )


@dataclass(frozen=True, slots=True)
class OutboxDispatchResult:
    run_id: str
    seq: int
    proposal_index: int
    status: str
    intent_id: str | None
    reason_codes: tuple[str, ...] = ()


class OutboxDispatchError(RuntimeError):
    """An outbox item could not be safely bound to its authoritative event."""


class PythonOutboxIntentHandoff:
    """Authorize committed proposals and idempotently materialize ledger intents.

    The class intentionally stops at a CREATED ledger intent. It does not
    reserve cash or submit orders; those actions require refreshed risk facts
    and a separate SEND_STARTED recovery boundary.
    """

    def __init__(
        self,
        *,
        journal: SQLiteEventJournal,
        ledger: SQLiteOrderLedger,
        authorize: ProposalAuthorizer,
    ) -> None:
        self.journal = journal
        self.ledger = ledger
        self.authorize = authorize

    def process_next(self, run_id: str, *, allow_paused: bool = False) -> OutboxDispatchResult | None:
        """Resolve one durable handoff.

        ``allow_paused`` is a trusted recovery-only escape hatch. Its caller
        must first hold the execution owner lease, reconcile a fresh account
        snapshot, and verify the committed prefix; it never authorizes reserve
        or send by itself.
        """
        work_items = self.journal.list_dispatchable_outbox(run_id, include_paused=allow_paused)
        if not work_items:
            return None
        item = work_items[0]
        seq = item["seq"]
        index = item["proposal_index"]
        proposal = OutboxProposal(
            run_id=run_id,
            event_id=item["event_id"],
            seq=seq,
            proposal_index=index,
            proposal=item["proposal"],
            identity=item["identity"],
            bar=_event_bar(item["event_payload"]),
        )

        if item["identity"]["environment"] != Environment.PAPER.value:
            if item["status"] != "PENDING":
                raise OutboxDispatchError("prepared non-paper intent requires manual reconciliation")
            reasons = ("live_execution_disabled",)
            self.journal.reject_outbox(
                run_id,
                seq,
                index,
                reason=reasons[0],
                decision={"allowed": False, "reason_codes": list(reasons), "policy": "paper_only"},
            )
            return OutboxDispatchResult(run_id, seq, index, "REJECTED", None, reasons)

        if item["status"] == "PENDING":
            authorized = self.authorize(proposal)
            if not authorized.decision.allowed:
                reasons = authorized.decision.reason_codes or ("risk_rejected",)
                self.journal.reject_outbox(
                    run_id,
                    seq,
                    index,
                    reason=",".join(reasons),
                    decision=authorized.decision.model_dump(mode="json"),
                )
                return OutboxDispatchResult(run_id, seq, index, "REJECTED", None, reasons)
            self._validate_authorization(proposal, authorized)
            if authorized.intent is None:
                raise OutboxDispatchError("risk-approved proposal did not produce an order intent")
            frozen = self.journal.prepare_outbox_intent(
                run_id,
                seq,
                index,
                intent=authorized.intent.model_dump(mode="json"),
                decision=authorized.decision.model_dump(mode="json"),
            )
        else:
            frozen = {
                "intent": item["prepared_intent"],
                "decision": item["decision"],
                "intent_id": item["intent_id"],
            }

        intent = OrderIntentInput.model_validate(frozen["intent"])
        decision = RiskDecision.model_validate(frozen["decision"])
        if not decision.allowed:
            raise OutboxDispatchError("frozen outbox handoff contains a non-approved decision")
        # RESERVED rows cross a second SQLite database boundary after the
        # authorization was frozen. Revalidate the immutable payload against
        # the exact event proposal and run identity before materializing it.
        self._validate_authorization(
            proposal,
            AuthorizedProposal(intent, decision, intent.broker, intent.reference_price),
        )
        # The decision and full intent were fixed before the cross-database
        # write. Reconstructing from this payload makes a crash retry exact.
        ledger_order: OrderRecord = self.ledger.create_intent(intent)
        if frozen["intent_id"] not in (None, ledger_order.intent_id):
            raise OutboxDispatchError("prepared outbox intent resolved to an unexpected ledger ID")
        self.journal.link_outbox_intent(run_id, seq, index, ledger_order.intent_id)
        return OutboxDispatchResult(
            run_id,
            seq,
            index,
            "LINKED",
            ledger_order.intent_id,
            decision.reason_codes,
        )

    @staticmethod
    def _validate_authorization(proposal: OutboxProposal, authorized: AuthorizedProposal) -> None:
        intent = authorized.intent
        if intent is None:
            raise OutboxDispatchError("authorized proposal has no order intent")
        decision = authorized.decision
        identity = proposal.identity
        expected = {
            "decision_id": f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            "deployment_id": identity["deployment_id"],
            "account_ref": identity["account_ref"],
            "environment": identity["environment"],
            "strategy_version_hash": identity["strategy_version_hash"],
            "input_event_seq": proposal.seq,
            "action_index": proposal.proposal_index,
            "bar_end": proposal.bar.end_at,
            "side": proposal.proposal["side"],
            "integer_qty": proposal.proposal["integer_qty"],
            "order_type": proposal.proposal["order_type"],
            "time_in_force": proposal.proposal["time_in_force"],
        }
        actual = {
            "decision_id": intent.decision_id,
            "deployment_id": intent.deployment_id,
            "account_ref": intent.account_ref,
            "environment": intent.environment.value,
            "strategy_version_hash": intent.strategy_version_hash,
            "input_event_seq": intent.input_event_seq,
            "action_index": intent.action_index,
            "bar_end": intent.bar_end,
            "side": intent.side,
            "integer_qty": intent.integer_qty,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
        }
        if actual != expected:
            raise OutboxDispatchError("authorized intent does not match host identity and worker proposal")
        if intent.broker != authorized.broker:
            raise OutboxDispatchError("intent broker does not match the authorizing grant")
        if intent.reference_price != authorized.reference_price:
            raise OutboxDispatchError("intent reference price does not match the authorized quote")
        if intent.instrument_id != proposal.bar.instrument_id:
            raise OutboxDispatchError("authorized instrument does not match the staged bar")
        if intent.reference_price <= 0 or not intent.risk_snapshot_id:
            raise OutboxDispatchError("authorized intent lacks a trusted reference price or risk snapshot")
        if intent.risk_snapshot_id != decision.risk_snapshot_id:
            raise OutboxDispatchError("intent risk snapshot does not match the host decision")


def _event_bar(payload: dict) -> Bar:
    if not isinstance(payload, dict) or set(payload) != {"bar"}:
        raise OutboxDispatchError("outbox event must contain exactly one staged bar")
    bar = Bar.model_validate(payload["bar"])
    if not bar.is_final:
        raise OutboxDispatchError("outbox event is not a completed bar")
    return bar
