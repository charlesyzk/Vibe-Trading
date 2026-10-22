from __future__ import annotations

import hashlib
import sqlite3

import pytest
from pydantic import ValidationError

from src.execution.contracts import Environment
from src.execution.event_journal import (
    EventNotReady,
    JournalError,
    ResultConflict,
    RunUnavailable,
    SQLiteEventJournal,
    WorkerEventResult,
)


VERSION_HASH = hashlib.sha256(b"immutable-strategy-version").hexdigest()


@pytest.fixture
def journal(tmp_path):
    value = SQLiteEventJournal(tmp_path / "execution.sqlite")
    value.create_run(
        run_id="run-1",
        deployment_id="deployment-authoritative",
        account_ref="paper-account-authoritative",
        environment=Environment.PAPER,
        strategy_version_hash=VERSION_HASH,
    )
    return value


def test_only_one_host_staged_event_can_be_outstanding(journal):
    with pytest.raises(EventNotReady):
        journal.release_next_event("run-1")

    first = journal.stage_next_event("run-1", {"bar": {"date": "2026-10-20", "close": "10.25"}}, event_id="event-1")
    assert (first.seq, first.event_id) == (1, "event-1")
    with pytest.raises(EventNotReady, match="must commit"):
        journal.stage_next_event("run-1", {"bar": {"date": "2026-10-21", "close": "11"}})

    released = journal.release_next_event("run-1")
    assert released.payload == {"bar": {"close": "10.25", "date": "2026-10-20"}}
    # A transport retry receives the same authoritative event, not a new one.
    assert journal.release_next_event("run-1") == released


def test_paused_released_event_can_only_resume_with_its_exact_durable_identity(journal):
    staged = journal.stage_next_event(
        "run-1", {"bar": {"date": "2026-10-20", "close": "10.25"}}, event_id="event-1",
    )
    original = journal.release_next_event("run-1")
    journal.pause_run("run-1")

    paused = journal.inspect_paused_released_event("run-1")
    assert paused == original
    assert paused.seq == staged.seq == 1
    assert paused.event_id == "event-1"
    assert len(paused.payload_sha256) == 64
    with pytest.raises(JournalError, match="expected released event"):
        journal.resume_paused_released_event(
            "run-1", expected_event_id="forged-event", expected_seq=1,
            expected_payload_sha256=paused.payload_sha256,
        )
    with pytest.raises(RunUnavailable, match="expected event cursor"):
        journal.resume_paused_released_event(
            "run-1", expected_event_id=paused.event_id, expected_seq=2,
            expected_payload_sha256=paused.payload_sha256,
        )
    assert journal.inspect_paused_released_event("run-1") == original

    retried = journal.resume_paused_released_event(
        "run-1", expected_event_id=paused.event_id, expected_seq=paused.seq,
        expected_payload_sha256=paused.payload_sha256,
    )
    assert retried == original
    assert journal.release_next_event("run-1") == original
    assert journal.get_checkpoint("run-1") == {}
    assert journal.commit_result("run-1", {"event_id": "event-1", "state": {"retried": True}})
    journal.pause_run("run-1")
    with pytest.raises(EventNotReady, match="no released event"):
        journal.inspect_paused_released_event("run-1")


def test_paused_released_event_recovery_rejects_corrupt_payload_or_side_effects(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    released = journal.release_next_event("run-1")
    journal.pause_run("run-1")
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE strategy_event SET payload_json=? WHERE run_id='run-1' AND seq=1",
            ('{"bar":{"close":11}}',),
        )
    with pytest.raises(JournalError, match="integrity"):
        journal.inspect_paused_released_event("run-1")
    with pytest.raises(JournalError, match="integrity"):
        journal.resume_paused_released_event(
            "run-1", expected_event_id=released.event_id, expected_seq=released.seq,
            expected_payload_sha256=released.payload_sha256,
        )
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE strategy_event SET payload_json=? WHERE run_id='run-1' AND seq=1",
            ('{"bar":{"close":10}}',),
        )
        connection.execute(
            "INSERT INTO order_outbox(run_id,seq,proposal_index,proposal_json) VALUES ('run-1',1,0,'{}')"
        )
    with pytest.raises(JournalError, match="outbox side effects"):
        journal.resume_paused_released_event(
            "run-1", expected_event_id=released.event_id, expected_seq=released.seq,
            expected_payload_sha256=released.payload_sha256,
        )
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0] == "PAUSED"


def test_result_checkpoint_and_outbox_commit_together_and_replay_idempotently(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    result = {
        "event_id": "event-1",
        "state": {"fast": "10", "slow": "9"},
        "order_proposals": [{"side": "BUY", "integer_qty": 3, "reason": "cross_up"}],
    }

    assert journal.commit_result("run-1", result) is True
    assert journal.get_checkpoint("run-1") == {"fast": "10", "slow": "9"}
    assert journal.list_pending_outbox("run-1") == [
        {
            "seq": 1,
            "proposal_index": 0,
            "proposal": {
                "integer_qty": 3,
                "order_type": "market",
                "reason": "cross_up",
                "side": "BUY",
                "time_in_force": "DAY",
            },
            "status": "PENDING",
        }
    ]
    assert journal.commit_result("run-1", result) is False

    next_event = journal.stage_next_event("run-1", {"bar": {"close": 11}}, event_id="event-2")
    assert next_event.seq == 2


def test_mid_commit_sqlite_failure_rolls_back_then_replayed_result_commits_once(journal):
    """A failure after the result/outbox/checkpoint writes must leave no partial commit.

    The trigger fires on the last journal mutation in ``commit_result``. Closing
    that real file-backed SQLite connection after the failed statement models
    loss of the caller before COMMIT; reopening proves SQLite rolled the whole
    transaction back. This is statement-failure evidence, not a claim about
    arbitrary process or storage-device failure points.
    """
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    result = {
        "event_id": "event-1",
        "state": {"fast": "10", "slow": "9"},
        "order_proposals": [{"side": "BUY", "integer_qty": 3, "reason": "cross_up"}],
    }
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_before_event_commit
               BEFORE UPDATE OF committed_at ON strategy_event
               WHEN NEW.committed_at IS NOT NULL
               BEGIN SELECT RAISE(ABORT, 'injected failure before event commit'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        journal.commit_result("run-1", result)

    reopened = SQLiteEventJournal(journal.path)
    with sqlite3.connect(journal.path) as connection:
        run = connection.execute(
            "SELECT state, next_seq, checkpoint_json FROM execution_run WHERE run_id='run-1'"
        ).fetchone()
        event = connection.execute(
            "SELECT released_at, committed_at FROM strategy_event WHERE run_id='run-1' AND seq=1"
        ).fetchone()
        result_count = connection.execute(
            "SELECT COUNT(*) FROM event_result WHERE run_id='run-1' AND seq=1"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-1' AND seq=1"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER fail_before_event_commit")

    assert run == ("RUNNING", 1, "{}")
    assert event[0] is not None and event[1] is None
    assert result_count == 0
    assert outbox_count == 0
    assert reopened.get_checkpoint("run-1") == {}

    assert reopened.commit_result("run-1", result) is True
    retry = SQLiteEventJournal(journal.path)
    assert retry.commit_result("run-1", result) is False
    assert retry.get_checkpoint("run-1") == {"fast": "10", "slow": "9"}
    assert len(retry.list_pending_outbox("run-1")) == 1


def test_checkpoint_is_verified_against_committed_result_after_reopen(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    result = {"event_id": "event-1", "state": {"counter": 1, "indicator": [10, 9]}}
    assert journal.commit_result("run-1", result) is True

    reopened = SQLiteEventJournal(journal.path)
    assert reopened.get_checkpoint("run-1") == {"counter": 1, "indicator": [10, 9]}
    assert reopened.commit_result("run-1", result) is False
    assert reopened.get_checkpoint("run-1") == {"counter": 1, "indicator": [10, 9]}


def test_conflicting_duplicate_pauses_run_and_rejects_future_events(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    assert journal.commit_result("run-1", {"event_id": "event-1", "state": {"n": 1}})

    with pytest.raises(ResultConflict, match="paused"):
        journal.commit_result("run-1", {"event_id": "event-1", "state": {"n": 2}})
    with pytest.raises(RunUnavailable):
        journal.stage_next_event("run-1", {"bar": {"close": 11}})


def test_worker_cannot_choose_account_authority_or_event_sequence(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    with pytest.raises(ValidationError):
        WorkerEventResult.model_validate(
            {"event_id": "event-1", "state": {}, "account_ref": "forged"}
        )
    with pytest.raises(ValidationError):
        WorkerEventResult.model_validate(
            {"event_id": "event-1", "state": {}, "seq": 999}
        )
    with pytest.raises(ResultConflict):
        journal.commit_result("run-1", {"event_id": "event-2", "state": {}})
    with pytest.raises(RunUnavailable):
        journal.release_next_event("run-1")


@pytest.mark.parametrize(
    "proposal",
    [
        {"side": "BUY", "integer_qty": True},
        {"side": "SHORT", "integer_qty": 1},
        {"side": "BUY", "integer_qty": 1.5},
        {"side": "BUY", "integer_qty": 1, "order_type": "limit"},
    ],
)
def test_worker_order_proposals_reject_invalid_side_quantity_and_order_style(proposal):
    with pytest.raises((ValidationError, ValueError)):
        WorkerEventResult.model_validate(
            {"event_id": "event-1", "state": {}, "order_proposals": [proposal]}
        )


def test_worker_result_size_is_bounded_and_never_advances_cursor(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    with pytest.raises(ValueError, match="byte limit"):
        journal.commit_result("run-1", {"event_id": "event-1", "state": {"large": "x" * 1_100_000}})
    with pytest.raises(EventNotReady, match="outstanding"):
        journal.stage_next_event("run-1", {"bar": {"close": 11}})


@pytest.mark.parametrize(
    ("checkpoint_json", "result_json", "result_sha256", "result_event_id"),
    [
        ('{"counter":2}', None, None, None),  # valid JSON, wrong state
        ('{"counter":1,"counter":2}', None, None, None),  # duplicate key
        ("{broken", None, None, None),  # malformed JSON
        (None, '{"event_id":"event-1","state":{"counter":2}}', None, None),
        (None, None, "0" * 64, None),  # result hash mismatch
        (None, None, None, "event-forged"),  # result/event identity mismatch
        (
            None,
            '{"event_id":"event-1","event_id":"forged","state":{"counter":1}}',
            hashlib.sha256(b'{"event_id":"event-1","event_id":"forged","state":{"counter":1}}').hexdigest(),
            None,
        ),  # duplicate result key despite a matching raw hash
        (
            None,
            '{"event_id":"event-forged","state":{"counter":1}}',
            hashlib.sha256(b'{"event_id":"event-forged","state":{"counter":1}}').hexdigest(),
            None,
        ),  # valid hash, but payload identity disagrees with event
    ],
)
def test_checkpoint_integrity_failure_persistently_pauses_run(
    journal, checkpoint_json, result_json, result_sha256, result_event_id,
):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    journal.commit_result("run-1", {"event_id": "event-1", "state": {"counter": 1}})
    with sqlite3.connect(journal.path) as connection:
        if checkpoint_json is not None:
            connection.execute(
                "UPDATE execution_run SET checkpoint_json=? WHERE run_id='run-1'", (checkpoint_json,)
            )
        if result_json is not None:
            connection.execute(
                "UPDATE event_result SET result_json=? WHERE run_id='run-1' AND seq=1", (result_json,)
            )
        if result_sha256 is not None:
            connection.execute(
                "UPDATE event_result SET result_sha256=? WHERE run_id='run-1' AND seq=1", (result_sha256,)
            )
        if result_event_id is not None:
            connection.execute(
                "UPDATE event_result SET event_id=? WHERE run_id='run-1' AND seq=1", (result_event_id,)
            )

    with pytest.raises(JournalError, match="integrity check failed; run paused"):
        journal.get_checkpoint("run-1")
    with sqlite3.connect(journal.path) as connection:
        state = connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0]
    assert state == "PAUSED"


def test_checkpoint_rejects_missing_prior_committed_result_and_pauses(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    journal.commit_result("run-1", {"event_id": "event-1", "state": {"counter": 1}})
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DELETE FROM event_result WHERE run_id='run-1' AND seq=1")

    with pytest.raises(JournalError, match="prior committed event result is missing"):
        journal.get_checkpoint("run-1")
    with pytest.raises(RunUnavailable):
        journal.get_run_identity("run-1")


def test_checkpoint_rejects_noncanonical_result_even_with_matching_hash(journal):
    journal.stage_next_event("run-1", {"bar": {"close": 10}}, event_id="event-1")
    journal.release_next_event("run-1")
    journal.commit_result("run-1", {"event_id": "event-1", "state": {"counter": 1}})
    noncanonical = '{"state":{"counter":1},"event_id":"event-1"}'
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE event_result SET result_json=?, result_sha256=? WHERE run_id='run-1' AND seq=1",
            (noncanonical, hashlib.sha256(noncanonical.encode("utf-8")).hexdigest()),
        )

    with pytest.raises(JournalError, match="result JSON is not canonical"):
        journal.get_checkpoint("run-1")
    with pytest.raises(RunUnavailable):
        journal.get_run_identity("run-1")


def test_checkpoint_rejects_noninteger_cursor_and_pauses(journal):
    with sqlite3.connect(journal.path) as connection:
        connection.execute("UPDATE execution_run SET next_seq=1.5 WHERE run_id='run-1'")

    with pytest.raises(JournalError, match="cursor is not an integer"):
        journal.get_checkpoint("run-1")
    with pytest.raises(RunUnavailable):
        journal.get_run_identity("run-1")
