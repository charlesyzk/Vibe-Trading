from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.execution.contracts import Bar, Environment, RiskGrant, canonical_json
from src.execution.event_journal import JournalError, RunUnavailable, SQLiteEventJournal
from src.execution.order_ledger import LocalOrderState, OrderIntentInput, SQLiteOrderLedger
from src.execution.outbox_dispatch import (
    AuthorizedProposal,
    OutboxDispatchError,
    ProposalRiskInputs,
    PythonOutboxIntentHandoff,
    RiskAwareProposalAuthorizer,
)
from src.execution.risk import PositionFact, RiskDecision, RiskFacts


NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
VERSION = hashlib.sha256(b"frozen-python-strategy").hexdigest()
RISK = hashlib.sha256(b"risk-snapshot").hexdigest()


def _bar() -> Bar:
    return Bar(
        instrument_id="US.TEST",
        session_date="2026-10-20",
        start_at=datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        end_at=NOW,
        available_at=NOW,
        timezone="America/New_York",
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("1000"),
        currency="USD",
        is_final=True,
        source="synthetic-fixture",
        adjustment="unadjusted",
    )


def _intent(**updates) -> OrderIntentInput:
    values = {
        "decision_id": "run-1:event-1:0",
        "broker": "FAKE",
        "account_ref": "paper-account-1",
        "environment": Environment.PAPER,
        "deployment_id": "deployment-1",
        "strategy_version_hash": VERSION,
        "input_event_seq": 1,
        "action_index": 0,
        "bar_end": NOW,
        "instrument_id": "US.TEST",
        "side": "BUY",
        "integer_qty": 2,
        "order_type": "market",
        "time_in_force": "DAY",
        "reference_price": Decimal("10"),
        "eligible_at": NOW + timedelta(days=1),
        "expires_at": NOW + timedelta(days=2),
        "risk_snapshot_id": RISK,
    }
    values.update(updates)
    return OrderIntentInput.model_validate(values)


def _decision(*, allowed: bool = True, reasons: tuple[str, ...] = ()) -> RiskDecision:
    return RiskDecision(
        allowed=allowed,
        reason_codes=reasons,
        checked_at=NOW,
        risk_snapshot_id=RISK,
        estimated_notional=Decimal("20"),
        estimated_cost=Decimal("20"),
        projected_strategy_exposure=Decimal("20"),
        projected_gross_exposure=Decimal("20"),
    )


def _committed_proposal(tmp_path, *, environment=Environment.PAPER):
    journal = SQLiteEventJournal(tmp_path / "journal.sqlite")
    journal.create_run(
        run_id="run-1",
        deployment_id="deployment-1",
        account_ref="paper-account-1",
        environment=environment,
        strategy_version_hash=VERSION,
    )
    journal.stage_next_event("run-1", {"bar": _bar().model_dump(mode="json")}, event_id="event-1")
    journal.release_next_event("run-1")
    journal.commit_result(
        "run-1",
        {
            "event_id": "event-1",
            "state": {"counter": 1},
            "order_proposals": [{"side": "BUY", "integer_qty": 2, "reason": "entry"}],
        },
    )
    return journal


def _authorizer(*, decision=None, intent=None, calls=None):
    def authorize(proposal):
        if calls is not None:
            calls.append(proposal)
        value = intent or _intent()
        return AuthorizedProposal(
            intent=value,
            decision=decision or _decision(),
            broker=value.broker,
            reference_price=value.reference_price,
        )

    return authorize


def _risk_inputs(*, max_order_notional="1000") -> ProposalRiskInputs:
    grant = RiskGrant.model_validate(
        {
            "grant_id": "grant-1",
            "user_action_id": "user-action-1",
            "strategy_version_hash": VERSION,
            "broker": "FAKE",
            "account_ref": "paper-account-1",
            "environment": "paper",
            "allowed_instruments": ["US.TEST"],
            "allowed_order_types": ["market"],
            "long_only": True,
            "whole_shares": True,
            "max_order_notional": max_order_notional,
            "max_gross_exposure": "1000",
            "strategy_budget": "500",
            "max_daily_loss": "100",
            "max_orders_per_session": 5,
            "valid_from": NOW - timedelta(minutes=5),
            "valid_until": NOW + timedelta(hours=1),
            "quote_max_age_seconds": 30,
            "account_max_age_seconds": 60,
            "halt_action": "pause_and_cancel_owned",
            "liquidation_policy": "none",
            "granted_at": NOW,
        }
    )
    facts = RiskFacts(
        broker="FAKE",
        account_ref="paper-account-1",
        environment=Environment.PAPER,
        strategy_version_hash=VERSION,
        instrument_id="US.TEST",
        now=NOW,
        quote_as_of=NOW - timedelta(seconds=1),
        account_as_of=NOW - timedelta(seconds=2),
        quote_price=Decimal("10"),
        price_buffer_fraction=Decimal("0.01"),
        estimated_fees=Decimal("0.10"),
        net_liquidation=Decimal("1000"),
        available_cash=Decimal("500"),
        positions=(PositionFact(instrument_id="US.TEST", quantity=0, market_price=Decimal("10")),),
        strategy_position_qty=0,
        strategy_buy_reservations=Decimal("0"),
        account_buy_reservations=Decimal("0"),
        strategy_daily_loss=Decimal("0"),
        orders_this_session=0,
        account_snapshot_complete=True,
    )
    return ProposalRiskInputs(
        grant=grant,
        facts=facts,
        eligible_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
    )


def test_authorized_proposal_is_frozen_and_linked_to_created_ledger_intent(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=_authorizer())

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "LINKED"
    assert ledger.get_order(result.intent_id).state is LocalOrderState.CREATED
    assert ledger.get_intent(result.intent_id) == _intent()
    assert journal.list_dispatchable_outbox("run-1") == []
    assert dispatch.process_next("run-1") is None


def test_risk_rejection_is_terminal_and_creates_no_intent(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=_authorizer(decision=_decision(allowed=False, reasons=("max_order_notional",))),
    )

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "REJECTED"
    assert result.reason_codes == ("max_order_notional",)
    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert journal.list_pending_outbox("run-1") == []


def test_risk_aware_authorizer_evaluates_host_grant_and_builds_bound_intent(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    authorize = RiskAwareProposalAuthorizer(lambda proposal: _risk_inputs())
    dispatch = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "LINKED"
    intent = ledger.get_intent(result.intent_id)
    assert intent.account_ref == "paper-account-1"
    assert intent.strategy_version_hash == VERSION
    assert intent.instrument_id == "US.TEST"
    assert intent.integer_qty == 2
    assert len(intent.risk_snapshot_id) == 64


def test_risk_aware_authorizer_rejects_over_limit_without_creating_intent(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    authorize = RiskAwareProposalAuthorizer(lambda proposal: _risk_inputs(max_order_notional="10"))
    dispatch = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "REJECTED"
    assert "max_order_notional" in result.reason_codes
    assert ledger.recovery_snapshot().ready_to_reserve == ()


@pytest.mark.parametrize(
    "grant_update, facts_update, expected_reason",
    [
        ({"account_ref": "other-account"}, {"account_ref": "other-account"}, "grant_account_mismatch"),
        ({"environment": Environment.LIVE}, {"environment": Environment.LIVE}, "grant_environment_mismatch"),
        ({"strategy_version_hash": "b" * 64}, {"strategy_version_hash": "b" * 64}, "grant_strategy_version_mismatch"),
    ],
)
def test_grant_and_risk_facts_must_bind_to_journal_run(
    tmp_path, grant_update, facts_update, expected_reason
):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    base = _risk_inputs()
    context = ProposalRiskInputs(
        grant=base.grant.model_copy(update=grant_update),
        facts=base.facts.model_copy(update=facts_update),
        eligible_at=base.eligible_at,
        expires_at=base.expires_at,
    )
    authorize = RiskAwareProposalAuthorizer(lambda proposal: context)
    dispatch = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "REJECTED"
    assert expected_reason in result.reason_codes
    assert ledger.recovery_snapshot().ready_to_reserve == ()


def test_authorizer_cannot_override_run_account_or_worker_order(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    forged = _intent(account_ref="different-account")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=_authorizer(intent=forged),
    )

    with pytest.raises(OutboxDispatchError, match="host identity"):
        dispatch.process_next("run-1")

    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert journal.list_pending_outbox("run-1")[0]["status"] == "PENDING"


def test_live_run_is_terminally_rejected_before_authorizer_or_ledger(tmp_path):
    journal = _committed_proposal(tmp_path, environment=Environment.LIVE)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=lambda proposal: pytest.fail("live run must be blocked before authorization"),
    )

    result = dispatch.process_next("run-1")

    assert result is not None and result.status == "REJECTED"
    assert result.reason_codes == ("live_execution_disabled",)
    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert journal.list_pending_outbox("run-1") == []


def test_modified_staged_event_payload_pauses_run_before_authorization(tmp_path):
    journal = _committed_proposal(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE strategy_event SET payload_json=? WHERE run_id=? AND seq=?",
            ('{"bar":{"instrument_id":"FORGED"}}', "run-1", 1),
        )
        connection.commit()
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=lambda proposal: pytest.fail("corrupted event must not reach authorization"),
    )

    with pytest.raises(JournalError, match="integrity check failed"):
        dispatch.process_next("run-1")

    with sqlite3.connect(journal.path) as connection:
        state = connection.execute("SELECT state FROM execution_run WHERE run_id='run-1'").fetchone()[0]
    assert state == "PAUSED"
    assert ledger.recovery_snapshot().ready_to_reserve == ()


def test_modified_outbox_proposal_pauses_run_before_authorization(tmp_path):
    journal = _committed_proposal(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE order_outbox SET proposal_json=? WHERE run_id=? AND seq=? AND proposal_index=?",
            ('{"side":"BUY","integer_qty":99,"order_type":"market","time_in_force":"DAY"}',
             "run-1", 1, 0),
        )
        connection.commit()
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=lambda proposal: pytest.fail("corrupted proposal must not reach authorization"),
    )

    with pytest.raises(JournalError, match="outbox proposal"):
        dispatch.process_next("run-1")

    with sqlite3.connect(journal.path) as connection:
        state = connection.execute("SELECT state FROM execution_run WHERE run_id='run-1'").fetchone()[0]
    assert state == "PAUSED"
    assert ledger.recovery_snapshot().ready_to_reserve == ()


@pytest.mark.parametrize(
    "intent, broker, quote, match",
    [
        (_intent(decision_id="forged-decision"), "FAKE", Decimal("10"), "host identity"),
        (_intent(), "IBKR", Decimal("10"), "authorizing grant"),
        (_intent(), "FAKE", Decimal("11"), "authorized quote"),
    ],
)
def test_intent_audit_fields_must_match_authorization(tmp_path, intent, broker, quote, match):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=lambda proposal: AuthorizedProposal(
            intent=intent,
            decision=_decision(),
            broker=broker,
            reference_price=quote,
        ),
    )

    with pytest.raises(OutboxDispatchError, match=match):
        dispatch.process_next("run-1")

    assert ledger.recovery_snapshot().ready_to_reserve == ()


def test_crash_after_ledger_insert_replays_frozen_intent_without_reauthorization(tmp_path, monkeypatch):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    calls = []
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=_authorizer(calls=calls),
    )
    original_link = journal.link_outbox_intent

    def fail_before_journal_link(*args, **kwargs):
        raise RuntimeError("simulated process crash before journal link")

    monkeypatch.setattr(journal, "link_outbox_intent", fail_before_journal_link)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        dispatch.process_next("run-1")
    assert len(ledger.recovery_snapshot().ready_to_reserve) == 1
    original_intent_id = ledger.recovery_snapshot().ready_to_reserve[0].intent_id

    monkeypatch.setattr(journal, "link_outbox_intent", original_link)
    recovered = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=lambda proposal: pytest.fail("recovery must reuse frozen authorization"),
    )
    result = recovered.process_next("run-1")

    assert result is not None and result.intent_id == original_intent_id
    assert len(ledger.recovery_snapshot().ready_to_reserve) == 1
    assert journal.list_dispatchable_outbox("run-1") == []


def test_prepared_handoff_is_revalidated_before_ledger_insert(tmp_path, monkeypatch):
    journal = _committed_proposal(tmp_path)
    database = tmp_path / "journal.sqlite"
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    dispatch = PythonOutboxIntentHandoff(
        journal=journal,
        ledger=ledger,
        authorize=_authorizer(),
    )
    original_create = ledger.create_intent
    monkeypatch.setattr(
        ledger, "create_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash before ledger insert")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatch.process_next("run-1")

    # Model a damaged/tampered prepared row after prepare committed but before
    # the ledger insert retry. Identity must be checked before cross-store write.
    tampered_intent_json = canonical_json(_intent(account_ref="other-account"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE order_outbox SET prepared_intent_json=?, prepared_intent_sha256=? "
            "WHERE run_id='run-1' AND seq=1 AND proposal_index=0",
            (tampered_intent_json, hashlib.sha256(tampered_intent_json.encode("utf-8")).hexdigest()),
        )
    monkeypatch.setattr(ledger, "create_intent", original_create)
    recovered = PythonOutboxIntentHandoff(
        journal=journal, ledger=ledger,
        authorize=lambda _proposal: pytest.fail("prepared recovery must not reauthorize"),
    )

    with pytest.raises(OutboxDispatchError, match="does not match host identity"):
        recovered.process_next("run-1")
    assert ledger.recovery_snapshot().ready_to_reserve == ()


def test_prepared_handoff_hash_mismatch_pauses_run_before_ledger_insert(tmp_path, monkeypatch):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    original_create = ledger.create_intent
    monkeypatch.setattr(
        ledger, "create_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        PythonOutboxIntentHandoff(
            journal=journal, ledger=ledger, authorize=_authorizer(),
        ).process_next("run-1")

    with sqlite3.connect(tmp_path / "journal.sqlite") as connection:
        connection.execute(
            "UPDATE order_outbox SET prepared_intent_json=? WHERE run_id='run-1' AND seq=1 AND proposal_index=0",
            (canonical_json(_intent(account_ref="other-account")),),
        )
    monkeypatch.setattr(ledger, "create_intent", original_create)

    with pytest.raises(JournalError, match="prepared outbox intent integrity"):
        PythonOutboxIntentHandoff(
            journal=journal, ledger=ledger,
            authorize=lambda _proposal: pytest.fail("prepared recovery must not reauthorize"),
        ).process_next("run-1")
    assert ledger.recovery_snapshot().ready_to_reserve == ()
    with pytest.raises(RunUnavailable, match="run is not running"):
        journal.get_run_identity("run-1")


def test_two_consumers_link_one_logical_ledger_intent(tmp_path):
    journal = _committed_proposal(tmp_path)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    barrier = Barrier(2)

    def authorize(proposal):
        barrier.wait(timeout=5)
        intent = _intent()
        return AuthorizedProposal(
            intent=intent,
            decision=_decision(),
            broker=intent.broker,
            reference_price=intent.reference_price,
        )

    consumers = [
        PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.process_next("run-1"), consumers))

    assert len({result.intent_id for result in results if result is not None}) == 1
    assert len(ledger.recovery_snapshot().ready_to_reserve) == 1
    assert journal.list_dispatchable_outbox("run-1") == []
