"""Fail-closed composition of completed Python strategy events and BT orders.

This bridge is deliberately single-order and single-event. It is a host-owned
composition seam for offline integration tests; recovery of native orders,
positions, cash, and broker callbacks must still be proven before a restarted
runtime can activate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from src.execution.backtrader_dispatch import OneShotLedgerIntentResolver
from src.execution.contracts import Bar, Environment
from src.execution.event_journal import EventNotReady, SQLiteEventJournal
from src.execution.event_worker import EventWorkerOutcome, PythonEventCoordinator
from src.execution.execution_service import PaperExecutionService
from src.execution.order_ledger import ExecutionControlState, OrderIntentInput, SQLiteOrderLedger
from src.execution.outbox_dispatch import PythonOutboxIntentHandoff


class PaperEventBridgeError(RuntimeError):
    """The current bar cannot safely become a paper order."""


@dataclass(frozen=True, slots=True)
class PaperEventBridgeResult:
    outcome: EventWorkerOutcome
    intent_id: str | None
    native_order: Any | None


class PythonPaperEventBridge:
    """Run one trusted completed-bar event and bind at most one BT order."""

    def __init__(
        self,
        *,
        run_id: str,
        journal: SQLiteEventJournal,
        ledger: SQLiteOrderLedger,
        coordinator: PythonEventCoordinator,
        handoff: PythonOutboxIntentHandoff,
        execution_service: PaperExecutionService,
        intent_resolver: OneShotLedgerIntentResolver,
    ) -> None:
        self.run_id = run_id
        self.journal = journal
        self.ledger = ledger
        self.coordinator = coordinator
        self.handoff = handoff
        self.execution_service = execution_service
        self.intent_resolver = intent_resolver

    def process_completed_bar(self, strategy: Any, data: Any, bar: Bar) -> PaperEventBridgeResult:
        """Execute the current bar and synchronously create its native order.

        ``strategy`` must be the current Cerebro strategy instance and ``bar``
        must be supplied by the trusted host/feed adapter. The strategy worker
        cannot choose run identity, sequence, event ID, or ledger intent ID.
        """
        try:
            self._require_running_scope()
            bar = bar if isinstance(bar, Bar) else Bar.model_validate(bar)
            if not bar.is_final:
                raise PaperEventBridgeError("only a completed bar can enter the paper event bridge")
            feed_instrument = getattr(data, "_name", None)
            if feed_instrument and feed_instrument != bar.instrument_id:
                raise PaperEventBridgeError("current Backtrader feed differs from the completed bar instrument")

            try:
                outstanding = self.journal.peek_next_event(self.run_id)
            except EventNotReady:
                outstanding = None
            if outstanding is not None:
                if set(outstanding.payload) != {"bar"} or Bar.model_validate(outstanding.payload["bar"]) != bar:
                    raise PaperEventBridgeError(
                        "durable outstanding event differs from the current completed bar"
                    )

            outcome = self.coordinator.process_next(
                self.run_id,
                next_bar=bar,
                event_id=f"{self.run_id}:{uuid.uuid4().hex}",
            )
            released_bar = self._released_bar(outcome)
            if released_bar != bar:
                raise PaperEventBridgeError(
                    "durable event does not match the current completed bar; reconcile before continuing"
                )

            proposals = outcome.result.order_proposals
            if len(proposals) > 1:
                raise PaperEventBridgeError("paper event bridge supports at most one order proposal")
            if not proposals:
                if self._dispatchable_rows():
                    raise PaperEventBridgeError("unresolved prior outbox work blocks the next event")
                return PaperEventBridgeResult(outcome, None, None)
            if proposals[0].side not in {"BUY", "SELL"}:
                raise PaperEventBridgeError("paper event bridge received an unsupported order side")

            rows = self._dispatchable_rows()
            if len(rows) != 1 or rows[0]["event_id"] != outcome.event.event_id or rows[0]["seq"] != outcome.event.seq:
                raise PaperEventBridgeError("stale or ambiguous outbox work blocks order dispatch")

            handoff_result = self.handoff.process_next(self.run_id)
            if (
                handoff_result is None
                or handoff_result.status != "LINKED"
                or handoff_result.proposal_index != 0
                or not handoff_result.intent_id
            ):
                raise PaperEventBridgeError("proposal did not resolve to one durable ledger intent")

            intent = self.ledger.get_intent(handoff_result.intent_id)
            self._validate_intent(intent, outcome, bar, proposals[0])
            with self.intent_resolver.bind_next(handoff_result.intent_id):
                if intent.side == "BUY":
                    native_order = strategy.buy(data=data, size=intent.integer_qty)
                elif intent.side == "SELL":
                    native_order = strategy.sell(data=data, size=intent.integer_qty)
                else:  # The durable intent model currently permits BUY/SELL only.
                    raise PaperEventBridgeError("durable intent has an unsupported order side")
            return PaperEventBridgeResult(outcome, handoff_result.intent_id, native_order)
        except Exception as exc:
            self._pause_fail_closed()
            if isinstance(exc, PaperEventBridgeError):
                raise
            raise PaperEventBridgeError(f"paper event bridge paused after a fail-closed error: {exc}") from exc

    def _require_running_scope(self) -> None:
        service = self.execution_service
        if not service.lease_held:
            raise PaperEventBridgeError("execution owner lease is not held")
        service.require_owner_lease()
        state, _reason = service.state()
        if state is not ExecutionControlState.RUNNING:
            raise PaperEventBridgeError("paper execution scope is not RUNNING")
        identity = self.journal.get_run_identity(self.run_id)
        if identity["environment"] != Environment.PAPER.value:
            raise PaperEventBridgeError("Python paper bridge rejects non-paper runs")
        if identity["account_ref"] != service.account_ref:
            raise PaperEventBridgeError("run account differs from the execution service scope")
        if self.journal.list_dispatchable_outbox(self.run_id):
            raise PaperEventBridgeError("unresolved prior outbox work blocks strategy worker execution")
        recovery = self.ledger.recovery_snapshot_for_scope(
            broker=service.broker,
            account_ref=service.account_ref,
            environment=service.environment,
        )
        if recovery.ready_to_reserve or recovery.ready_to_send or recovery.unresolved_sends or recovery.active_orders:
            raise PaperEventBridgeError("unreconciled order ledger work blocks the next strategy event")

    def _dispatchable_rows(self) -> list[dict[str, Any]]:
        return self.journal.list_dispatchable_outbox(self.run_id)

    @staticmethod
    def _released_bar(outcome: EventWorkerOutcome) -> Bar:
        payload = outcome.event.payload
        if set(payload) != {"bar"}:
            raise PaperEventBridgeError("durable strategy event has an invalid payload")
        return Bar.model_validate(payload["bar"])

    @staticmethod
    def _validate_intent(intent: OrderIntentInput, outcome: EventWorkerOutcome, bar: Bar, proposal: Any) -> None:
        expected = {
            "input_event_seq": outcome.event.seq,
            "bar_end": bar.end_at,
            "instrument_id": bar.instrument_id,
            "side": proposal.side,
            "integer_qty": proposal.integer_qty,
            "order_type": proposal.order_type,
            "time_in_force": proposal.time_in_force,
            "decision_id": f"{outcome.event.run_id}:{outcome.event.event_id}:0",
        }
        mismatches = [key for key, value in expected.items() if getattr(intent, key) != value]
        if mismatches:
            raise PaperEventBridgeError("durable intent differs from current event: " + ",".join(mismatches))

    def _pause_fail_closed(self) -> None:
        try:
            self.journal.pause_run(self.run_id)
        except Exception:
            pass
        try:
            state, _reason = self.execution_service.state()
            if state is ExecutionControlState.RUNNING and self.execution_service.lease_held:
                self.execution_service.pause(reason="paper_event_bridge_fail_closed")
        except Exception:
            pass
