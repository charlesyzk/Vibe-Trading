from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from contextlib import closing
import sqlite3

import pytest
from pydantic import ValidationError

from src.execution.contracts import Environment
from src.execution.order_ledger import (
    BrokerEventKind,
    ExecutionControlState,
    InvalidTransition,
    InvalidActivityCursor,
    LedgerConflict,
    LocalOrderState,
    OrderIntentInput,
    SQLiteOrderLedger,
)


VERSION = hashlib.sha256(b"offline-version").hexdigest()
RISK = hashlib.sha256(b"offline-risk-snapshot").hexdigest()
NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path):
    return SQLiteOrderLedger(tmp_path / "ledger.sqlite")


def intent(**updates):
    data = {
        "decision_id": "decision-1",
        "broker": "FAKE",
        "account_ref": "paper-account-fixture",
        "environment": Environment.PAPER,
        "deployment_id": "deployment-1",
        "strategy_version_hash": VERSION,
        "input_event_seq": 42,
        "action_index": 0,
        "bar_end": NOW - timedelta(hours=1),
        "instrument_id": "fixture-equity-1",
        "side": "BUY",
        "integer_qty": 10,
        "order_type": "market",
        "time_in_force": "DAY",
        "reference_price": Decimal("12.345"),
        "eligible_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "risk_snapshot_id": RISK,
    }
    data.update(updates)
    return data


def report(intent_id, kind, event_id, **updates):
    data = {
        "broker": "FAKE",
        "account_ref": "paper-account-fixture",
        "environment": Environment.PAPER,
        "intent_id": intent_id,
        "broker_event_id": event_id,
        "kind": kind,
        "occurred_at": NOW,
        "received_at": NOW + timedelta(seconds=1),
    }
    data.update(updates)
    return data


def test_logical_event_action_is_idempotent_and_conflicting_replay_fails(ledger):
    first = ledger.create_intent(intent())
    replay = ledger.create_intent(intent())
    assert replay == first
    assert first.state is LocalOrderState.CREATED
    assert first.dedupe_key == ledger.get_order(first.intent_id).dedupe_key

    with pytest.raises(LedgerConflict, match="different content"):
        ledger.create_intent(intent(integer_qty=11))

    next_action = ledger.create_intent(intent(action_index=1))
    next_event = ledger.create_intent(intent(input_event_seq=43))
    next_run = ledger.create_intent(intent(decision_id="decision-2"))
    assert len({first.intent_id, next_action.intent_id, next_event.intent_id, next_run.intent_id}) == 4


@pytest.mark.parametrize(
    "updates",
    [
        {"side": "SHORT"},
        {"integer_qty": True},
        {"integer_qty": 1.5},
        {"reference_price": "Infinity"},
        {"order_type": "limit"},
        {"eligible_at": datetime(2026, 10, 20)},
    ],
)
def test_intent_contract_fails_closed(updates):
    with pytest.raises((ValidationError, ValueError)):
        OrderIntentInput.model_validate(intent(**updates))


def test_reservation_is_atomic_and_idempotent_only_for_same_values(ledger):
    order = ledger.create_intent(intent())
    reserved = ledger.reserve(order.intent_id, "reservation-1", "123.4500")
    assert reserved.state is LocalOrderState.RESERVED
    assert reserved.reserved_amount == Decimal("123.4500")
    assert ledger.reserve(order.intent_id, "reservation-1", Decimal("123.45")) == reserved
    with pytest.raises(LedgerConflict):
        ledger.reserve(order.intent_id, "reservation-other", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    with pytest.raises(InvalidTransition):
        ledger.reserve(order.intent_id, "reservation-1", "123.45")


def test_send_started_is_persisted_once_and_unknown_never_becomes_sendable(tmp_path):
    db = tmp_path / "ledger.sqlite"
    ledger = SQLiteOrderLedger(db)
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    started = ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    assert started.state is LocalOrderState.SEND_STARTED

    # Reopen to simulate process restart after committing SEND_STARTED and
    # before the caller can establish whether the external call was delivered.
    recovered = SQLiteOrderLedger(db)
    snapshot = recovered.recovery_snapshot()
    assert snapshot.ready_to_send == ()
    assert [row.intent_id for row in snapshot.unresolved_sends] == [order.intent_id]
    assert [row.intent_id for row in snapshot.active_orders] == [order.intent_id]
    with pytest.raises(InvalidTransition):
        recovered.start_send(order.intent_id, attempt_id="attempt-2", client_order_id="client-2")

    unknown = recovered.mark_unknown(order.intent_id)
    assert unknown.state is LocalOrderState.UNKNOWN
    assert recovered.recovery_snapshot().ready_to_send == ()
    with pytest.raises(InvalidTransition):
        recovered.start_send(order.intent_id, attempt_id="attempt-3", client_order_id="client-3")


def test_legacy_active_sell_without_share_hold_freezes_scope_for_reconciliation(tmp_path):
    db = tmp_path / "legacy-sell.sqlite"
    ledger = SQLiteOrderLedger(db)
    order = ledger.create_intent(intent(side="SELL", integer_qty=4))
    ledger.set_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    with closing(ledger._connect()) as connection:
        connection.execute(
            "UPDATE order_intent SET state='UNKNOWN',reserved_amount='0' WHERE intent_id=?",
            (order.intent_id,),
        )
        connection.execute("DROP TABLE share_reservation")
        connection.commit()

    # Reopening applies the old-schema table addition and must freeze active
    # legacy SELL work whose quantity reservation cannot be reconstructed.
    recovered = SQLiteOrderLedger(db)

    assert recovered.get_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    ) == (ExecutionControlState.RECOVERING, "legacy_sell_share_reservation_missing")


def test_ack_and_fill_events_are_idempotent_by_event_and_execution_id(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ack = report(order.intent_id, BrokerEventKind.ACK, "evt-ack", broker_order_id="fake-ord-7")
    assert ledger.record_broker_event(ack) is True
    assert ledger.record_broker_event(ack) is False
    assert ledger.get_order(order.intent_id).state is LocalOrderState.ACKNOWLEDGED

    fill = report(
        order.intent_id, BrokerEventKind.FILL, "evt-fill-1", broker_exec_id="exec-1",
        quantity=4, price=Decimal("12.50"), broker_order_id="fake-ord-7",
    )
    assert ledger.record_broker_event(fill) is True
    assert ledger.record_broker_event(fill) is False
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert ledger.get_order(order.intent_id).remaining_qty == 6
    assert ledger.get_order(order.intent_id).reserved_amount == Decimal("74.07")
    assert ledger.get_order(order.intent_id).average_fill_price == Decimal("12.50")

    # A different callback event carrying the same execution ID is also
    # idempotent and does not add a second position effect.
    duplicate_exec = {**fill, "broker_event_id": "evt-fill-duplicate"}
    assert ledger.record_broker_event(duplicate_exec) is False
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    with pytest.raises(LedgerConflict, match="execution ID"):
        ledger.record_broker_event({**duplicate_exec, "broker_event_id": "evt-fill-other", "quantity": 5})


def test_scoped_activity_includes_terminal_orders_fills_and_reported_fees(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.ACK, "ack-1", broker_order_id="broker-1",
    ))
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.FILL, "fill-1", broker_order_id="broker-1",
        broker_exec_id="exec-1", quantity=10, price=Decimal("12.50"),
    ))
    ledger.append_fee_fact(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        broker_exec_id="exec-1", amount=Decimal("0.35"), currency="USD",
        received_at=NOW + timedelta(minutes=1), evidence={"source": "fixture"},
    )
    other_account = ledger.create_intent(intent(
        decision_id="other-account-decision", account_ref="different-paper-account",
    ))

    activity = ledger.list_activity_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    )

    assert [row["intent_id"] for row in activity] == [order.intent_id]
    assert activity[0]["state"] == "FILLED"
    assert "account_ref" not in activity[0]["intent"]
    assert activity[0]["filled_qty"] == 10
    assert activity[0]["fills"][0] == {
        "broker_exec_id": "exec-1", "quantity": 10, "price": "12.50",
        "occurred_at": NOW.isoformat(),
    }
    assert len(activity[0]["fees"]) == 1
    assert activity[0]["fees"][0]["amount_status"] == "REPORTED_UNCONFIRMED"
    assert "account_ref" not in activity[0]["fees"][0]
    assert "account_ref" not in activity[0]["fills"][0]
    assert other_account.intent_id not in {row["intent_id"] for row in activity}


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_scoped_activity_rejects_unbounded_or_non_integer_limits(ledger, limit):
    with pytest.raises(ValueError, match="limit must be an integer"):
        ledger.list_activity_for_scope(
            broker="FAKE", account_ref="paper-account-fixture",
            environment=Environment.PAPER, limit=limit,
        )


def test_activity_stream_replays_scope_local_sequences_after_ledger_reopen(ledger):
    order = ledger.create_intent(intent())
    ledger.create_intent(intent(account_ref="another-paper-account"))
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ack = report(order.intent_id, BrokerEventKind.ACK, "ack-stream", broker_order_id="broker-1")
    assert ledger.record_broker_event(ack) is True
    fill = report(
        order.intent_id, BrokerEventKind.FILL, "fill-stream", broker_order_id="broker-1",
        broker_exec_id="exec-stream", quantity=3, price=Decimal("12.50"),
    )
    assert ledger.record_broker_event(fill) is True
    fee = {
        "broker": "FAKE", "account_ref": "paper-account-fixture",
        "environment": Environment.PAPER, "broker_exec_id": "exec-stream",
        "amount": Decimal("0.35"), "currency": "USD", "received_at": NOW + timedelta(minutes=1),
        "evidence": {"source": "fixture"},
    }
    assert ledger.append_fee_fact(**fee) is True

    # Exact callback and fee replays are idempotent in both facts and cursor.
    assert ledger.record_broker_event(ack) is False
    assert ledger.record_broker_event(fill) is False
    assert ledger.append_fee_fact(**fee) is False
    last_seq = ledger.latest_activity_seq_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    )
    assert last_seq == 6

    reopened = SQLiteOrderLedger(ledger.path)
    page_one = reopened.read_activity_events_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        after_seq=0, limit=2,
    )
    page_two = reopened.read_activity_events_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        after_seq=2, limit=100,
    )
    all_events = page_one + page_two
    assert [event["seq"] for event in all_events] == [1, 2, 3, 4, 5, 6]
    assert [event["event_type"] for event in all_events] == [
        "order.created", "order.state", "order.state", "broker.report", "broker.report", "fee.reported",
    ]
    assert all("account_ref" not in event["payload"] for event in all_events)
    assert [event["seq"] for event in reopened.read_activity_events_for_scope(
        broker="FAKE", account_ref="another-paper-account", environment=Environment.PAPER,
        after_seq=0, limit=100,
    )] == [1]
    with closing(reopened._connect()) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE paper_activity_event SET event_type='tampered' "
                "WHERE broker='FAKE' AND account_ref='paper-account-fixture' AND seq=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM paper_activity_event "
                "WHERE broker='FAKE' AND account_ref='paper-account-fixture' AND seq=1"
            )


def test_activity_cursor_and_limit_fail_closed(ledger):
    ledger.create_intent(intent())
    with pytest.raises(InvalidActivityCursor, match="ahead"):
        ledger.read_activity_events_for_scope(
            broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
            after_seq=2, limit=10,
        )
    with pytest.raises(InvalidActivityCursor, match="non-negative"):
        ledger.read_activity_events_for_scope(
            broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
            after_seq=True, limit=10,
        )
    with pytest.raises(ValueError, match="between 1 and 100"):
        ledger.read_activity_events_for_scope(
            broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
            after_seq=0, limit=101,
        )


def test_activity_event_write_failure_rolls_back_order_and_broker_facts(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ack = report(order.intent_id, BrokerEventKind.ACK, "ack-rollback", broker_order_id="broker-1")
    with closing(ledger._connect()) as connection:
        connection.execute(
            "CREATE TRIGGER reject_activity BEFORE INSERT ON paper_activity_event "
            "BEGIN SELECT RAISE(ABORT, 'fixture activity sink failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="activity sink failure"):
        ledger.record_broker_event(ack)
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    with closing(ledger._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM broker_event").fetchone()[0] == 0
    assert ledger.latest_activity_seq_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    ) == 3


def test_activity_event_write_failure_rolls_back_fill_and_fee_facts(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.ACK, "ack-before-fill", broker_order_id="broker-1",
    ))
    with closing(ledger._connect()) as connection:
        connection.execute(
            "CREATE TRIGGER reject_activity BEFORE INSERT ON paper_activity_event "
            "BEGIN SELECT RAISE(ABORT, 'fixture activity sink failure'); END"
        )
    fill = report(
        order.intent_id, BrokerEventKind.FILL, "fill-rollback", broker_order_id="broker-1",
        broker_exec_id="exec-rollback", quantity=2, price=Decimal("12.50"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="activity sink failure"):
        ledger.record_broker_event(fill)
    assert ledger.get_order(order.intent_id).state is LocalOrderState.ACKNOWLEDGED
    assert ledger.list_fills(order.intent_id) == []

    with pytest.raises(sqlite3.IntegrityError, match="activity sink failure"):
        ledger.append_fee_fact(
            broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
            broker_exec_id="exec-rollback", amount="0.35", currency="USD",
            received_at=NOW + timedelta(minutes=1), evidence={"source": "fixture"},
        )
    with closing(ledger._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fee_fact").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM broker_event WHERE broker_event_id='fill-rollback'").fetchone()[0] == 0
    assert ledger.latest_activity_seq_for_scope(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    ) == 4


def test_reject_releases_reservation_and_duplicate_event_content_conflicts(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    rejected = report(order.intent_id, BrokerEventKind.REJECT, "evt-reject", message="fixture reject")
    assert ledger.record_broker_event(rejected)
    result = ledger.get_order(order.intent_id)
    assert result.state is LocalOrderState.REJECTED
    assert result.reserved_amount == 0
    assert ledger.record_broker_event(rejected) is False
    with pytest.raises(LedgerConflict, match="event ID"):
        ledger.record_broker_event({**rejected, "message": "different payload"})


def test_cancel_race_keeps_late_fill_fact_without_reopening_dispatch(ledger):
    order = ledger.create_intent(intent())
    ledger.set_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.ACK, "evt-ack", broker_order_id="ord-1"))
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.CANCELLED, "evt-cancel", broker_order_id="ord-1"))
    assert ledger.get_order(order.intent_id).state is LocalOrderState.CANCELLED

    late_fill = report(
        order.intent_id, BrokerEventKind.FILL, "evt-late-fill", broker_exec_id="exec-late",
        quantity=2, price=Decimal("12.75"), broker_order_id="ord-1",
    )
    assert ledger.record_broker_event(late_fill)
    final = ledger.get_order(order.intent_id)
    assert final.state is LocalOrderState.CANCELLED
    assert final.filled_qty == 2
    assert final.remaining_qty == 8
    assert final.reserved_amount == 0
    assert final.average_fill_price == Decimal("12.75")
    assert ledger.recovery_snapshot().ready_to_send == ()
    assert ledger.get_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    ) == (ExecutionControlState.RECOVERING, "late_fill_after_terminal_order")


def test_late_fill_after_reject_durably_pauses_execution(ledger):
    order = ledger.create_intent(intent())
    ledger.set_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.REJECT, "evt-reject"))

    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.FILL, "evt-late-fill", broker_exec_id="exec-late",
        quantity=2, price=Decimal("12.75"),
    ))

    assert ledger.get_order(order.intent_id).state is LocalOrderState.REJECTED
    assert ledger.get_order(order.intent_id).filled_qty == 2
    assert ledger.get_execution_control(
        broker="FAKE", account_ref="paper-account-fixture", environment=Environment.PAPER,
    ) == (ExecutionControlState.RECOVERING, "late_fill_after_terminal_order")


def test_ack_and_cancel_rejection_do_not_regress_partial_fill_facts(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.ACK, "evt-ack", broker_order_id="ord-1"))
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.FILL, "evt-fill", broker_exec_id="exec-1",
        quantity=3, price=Decimal("12.50"), broker_order_id="ord-1",
    ))
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.CANCEL_PENDING, "evt-cancel-pending", broker_order_id="ord-1"
    ))
    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.CANCEL_REJECTED, "evt-cancel-rejected", broker_order_id="ord-1"
    ))
    current = ledger.get_order(order.intent_id)
    assert current.state is LocalOrderState.PARTIALLY_FILLED
    assert current.filled_qty == 3
    assert current.remaining_qty == 7
    assert current.reserved_amount == Decimal("86.415")

    ledger.record_broker_event(report(
        order.intent_id, BrokerEventKind.CANCELLED, "evt-cancelled", broker_order_id="ord-1"
    ))
    with pytest.raises(LedgerConflict, match="cancel rejection conflicts"):
        ledger.record_broker_event(report(
            order.intent_id, BrokerEventKind.CANCEL_REJECTED, "evt-late-cancel-rejected", broker_order_id="ord-1"
        ))


def test_account_scope_mismatch_and_overfill_rollback_without_partial_write(ledger):
    order = ledger.create_intent(intent())
    ledger.reserve(order.intent_id, "reservation-1", "123.45")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    with pytest.raises(LedgerConflict, match="account or environment"):
        ledger.record_broker_event(report(
            order.intent_id, BrokerEventKind.ACK, "evt-wrong", account_ref="other-account"
        ))
    with pytest.raises(LedgerConflict, match="broker does not match"):
        ledger.record_broker_event(report(
            order.intent_id, BrokerEventKind.ACK, "evt-wrong-broker", broker="OTHER"
        ))
    with pytest.raises(LedgerConflict, match="exceed"):
        ledger.record_broker_event(report(
            order.intent_id, BrokerEventKind.FILL, "evt-overfill", broker_exec_id="exec-over",
            quantity=11, price=Decimal("12.50"),
        ))
    assert ledger.get_order(order.intent_id).filled_qty == 0
    assert ledger.list_fills(order.intent_id) == []
