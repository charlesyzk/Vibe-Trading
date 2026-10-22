from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3
from types import SimpleNamespace

import pytest

from src.execution.contracts import Bar, Environment, SourceKind, StrategySpec
from src.execution.event_journal import EventNotReady, JournalError, RunUnavailable, SQLiteEventJournal
from src.execution.event_worker import EventWorkerError, PythonEventCoordinator, invoke_strategy_worker
from src.execution.python_sandbox import SandboxStrategyFailure
from src.execution.versions import StrategyVersionStore


NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


def _bar(*, close: str = "12.50", is_final: bool = True):
    return {
        "instrument_id": "us_equity_demo",
        "session_date": date(2026, 10, 20),
        "start_at": NOW.replace(hour=13, minute=30),
        "end_at": NOW.replace(hour=20),
        "available_at": NOW,
        "timezone": "America/New_York",
        "open": Decimal("12.00"),
        "high": Decimal("13.00"),
        "low": Decimal("11.50"),
        "close": Decimal(close),
        "volume": Decimal("1000"),
        "currency": "USD",
        "is_final": is_final,
        "source": "synthetic_fixture",
        "adjustment": "unadjusted",
        "quality_flags": (),
    }


def _spec():
    return StrategySpec.model_validate({
        "schema_version": "1.0",
        "strategy_id": "uploaded_event_test",
        "source_kind": SourceKind.PYTHON_UPLOAD.value,
        "source_artifact_id": "upload-1",
        "instrument_ids": ["us_equity_demo"],
        "bar_policy": {
            "timeframe": "1d", "session": "regular", "completed_only": True,
            "calendar_id": "us_equity_rth", "timezone": "America/New_York",
        },
        "parameters": {},
        "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
        "execution_policy": {
            "decision": "completed_bar_available", "dispatch": "next_regular_session",
            "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
            "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause",
        },
    })


class _RecordingSandbox:
    def __init__(self, *, fail_first: bool = False):
        self.requests = []
        self.fail_first = fail_first

    def run_json(self, request):
        self.requests.append(request)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("worker exited before result")
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"seen": request["state"].get("seen", 0) + 1},
            "order_proposals": [{"side": "BUY", "integer_qty": 2, "reason": "fixture"}],
        })


def _setup(tmp_path, *, sandbox=None, source=None, initial_checkpoint=None, now=None):
    versions = StrategyVersionStore(tmp_path / "versions")
    source = source or b"def on_event(event, state):\n    return {'state': state, 'order_proposals': []}\n"
    version = versions.create_version(
        _spec(), upload_source=source, engine_version="backtrader/1.9.78.123",
        compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(tmp_path / "execution.sqlite")
    journal.create_run(
        run_id="run-1", deployment_id="deployment-1", account_ref="account-secret",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint=initial_checkpoint,
    )
    worker = sandbox or _RecordingSandbox()
    return PythonEventCoordinator(
        journal=journal, versions=versions, sandbox=worker, now=now,
    ), journal, worker


def test_paused_committed_prefix_shadow_replay_is_read_only_and_uses_seed_state(tmp_path):
    coordinator, journal, sandbox = _setup(
        tmp_path, initial_checkpoint={"seen": 10, "rolling": ["seed"]},
    )
    for index, close in enumerate(("12.50", "12.75", "13.00"), start=1):
        coordinator.process_next("run-1", next_bar=_bar(close=close), event_id=f"event-{index}")
    journal.pause_run("run-1")
    before = journal.read_committed_prefix("run-1")
    with sqlite3.connect(journal.path) as connection:
        before_next_seq = connection.execute(
            "SELECT next_seq FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0]
        before_outbox = connection.execute(
            "SELECT seq, proposal_index, proposal_json, status, intent_id FROM order_outbox "
            "WHERE run_id='run-1' ORDER BY seq, proposal_index"
        ).fetchall()
    sandbox.requests.clear()

    replayed = coordinator.shadow_replay_committed_prefix("run-1")

    assert len(replayed) == 3
    assert [request["event_id"] for request in sandbox.requests] == ["event-1", "event-2", "event-3"]
    assert [request["state"]["seen"] for request in sandbox.requests] == [10, 11, 12]
    assert sandbox.requests[0]["state"]["rolling"] == ["seed"]
    assert journal.read_committed_prefix("run-1") == before
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT next_seq FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0] == before_next_seq
        assert connection.execute(
            "SELECT seq, proposal_index, proposal_json, status, intent_id FROM order_outbox "
            "WHERE run_id='run-1' ORDER BY seq, proposal_index"
        ).fetchall() == before_outbox


def test_shadow_replay_of_tampered_middle_result_pauses_without_side_effects(tmp_path):
    import hashlib

    from src.execution.contracts import canonical_json

    coordinator, journal, _ = _setup(tmp_path, initial_checkpoint={"seen": 0})
    for index in range(1, 4):
        coordinator.process_next("run-1", next_bar=_bar(close=f"12.{index}0"), event_id=f"event-{index}")
    journal.pause_run("run-1")
    altered = canonical_json({
        "event_id": "event-2", "state": {"seen": 999},
        "order_proposals": [{"side": "BUY", "integer_qty": 2, "order_type": "market",
                              "time_in_force": "DAY", "reason": "fixture"}],
    })
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE event_result SET result_json=?, result_sha256=? WHERE run_id='run-1' AND seq=2",
            (altered, hashlib.sha256(altered.encode()).hexdigest()),
        )
        connection.execute(
            "UPDATE execution_run SET checkpoint_json=? WHERE run_id='run-1'", ('{"seen":3}',),
        )
        before_outbox_count = connection.execute(
            "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-1'"
        ).fetchone()[0]

    with pytest.raises(EventWorkerError, match="shadow replay failed|shadow replay differs"):
        coordinator.shadow_replay_committed_prefix("run-1")

    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0] == "PAUSED"
        assert connection.execute(
            "SELECT next_seq FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-1'"
        ).fetchone()[0] == before_outbox_count


def test_shadow_replay_rejects_legacy_run_without_initial_checkpoint(tmp_path):
    coordinator, journal, sandbox = _setup(tmp_path)
    coordinator.process_next("run-1", next_bar=_bar(), event_id="event-1")
    journal.pause_run("run-1")
    with sqlite3.connect(journal.path) as connection:
        connection.execute("UPDATE execution_run SET initial_checkpoint_json=NULL WHERE run_id='run-1'")

    with pytest.raises(EventWorkerError, match="shadow replay failed"):
        coordinator.shadow_replay_committed_prefix("run-1")

    assert len(sandbox.requests) == 1
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone()[0] == "PAUSED"


def test_coordinator_releases_one_completed_bar_and_commits_outbox(tmp_path):
    coordinator, journal, sandbox = _setup(tmp_path)

    outcome = coordinator.process_next("run-1", next_bar=_bar(), event_id="event-1")

    assert outcome.committed is True
    request = sandbox.requests[0]
    assert set(request) == {
        "op", "protocol_version", "request_id", "source", "event_id", "event", "state"
    }
    assert request["protocol_version"] == 1
    assert len(request["request_id"]) == 32
    assert request["event_id"] == "event-1"
    assert set(request["event"]) == {"bar"}
    assert request["event"]["bar"]["close"] == "12.50"
    assert request["state"] == {}
    assert not {
        "account_ref", "deployment_id", "seq", "next_event", "bars", "actor", "grant_id",
        "management_command", "command_id",
    } & set(request)
    assert journal.get_checkpoint("run-1") == {"seen": 1}
    assert journal.list_pending_outbox("run-1")[0]["proposal"]["integer_qty"] == 2
    with pytest.raises(EventNotReady):
        journal.release_next_event("run-1")


def test_worker_failure_retries_same_durable_event_before_new_bar(tmp_path):
    sandbox = _RecordingSandbox(fail_first=True)
    coordinator, journal, sandbox = _setup(tmp_path, sandbox=sandbox)

    with pytest.raises(RuntimeError, match="worker exited"):
        coordinator.process_next("run-1", next_bar=_bar(close="12.50"), event_id="event-1")

    recovered = coordinator.process_next("run-1", next_bar=_bar(close="99.00"), event_id="event-2")

    assert recovered.event.event_id == "event-1"
    assert recovered.event.seq == 1
    assert recovered.event.payload["bar"]["close"] == "12.50"
    assert sandbox.requests[0]["event_id"] == sandbox.requests[1]["event_id"] == "event-1"
    assert sandbox.requests[0]["request_id"] != sandbox.requests[1]["request_id"]
    assert journal.get_checkpoint("run-1") == {"seen": 1}


def test_tampered_online_checkpoint_pauses_before_worker_restarts(tmp_path):
    coordinator, journal, sandbox = _setup(tmp_path)
    coordinator.process_next("run-1", next_bar=_bar(close="12.50"), event_id="event-1")
    assert len(sandbox.requests) == 1

    # Simulate a legal-JSON disk alteration after event 1 committed and before
    # the reopened coordinator is allowed to process event 2.
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE execution_run SET checkpoint_json=? WHERE run_id='run-1'", ('{"seen":999}',)
        )

    reopened_journal = SQLiteEventJournal(journal.path)
    restarted = PythonEventCoordinator(
        journal=reopened_journal, versions=coordinator.versions, sandbox=sandbox,
    )
    with pytest.raises(JournalError, match="checkpoint does not match"):
        restarted.process_next("run-1", next_bar=_bar(close="12.75"), event_id="event-2")

    assert len(sandbox.requests) == 1
    with pytest.raises(RunUnavailable):
        reopened_journal.get_run_identity("run-1")


@pytest.mark.parametrize(
    "response_patch",
    [
        {"request_id": "0" * 32},
        {"protocol_version": 2},
        {"account_ref": "attacker-account"},
        {"management_command": "stop"},
    ],
)
def test_worker_ipc_rejects_stale_protocol_or_privileged_response_fields(tmp_path, response_patch):
    class TamperingSandbox(_RecordingSandbox):
        def run_json(self, request):
            response = super().run_json(request).response
            response.update(response_patch)
            return SimpleNamespace(response=response)

    coordinator, journal, sandbox = _setup(tmp_path, sandbox=TamperingSandbox())

    with pytest.raises(EventWorkerError, match="response"):
        coordinator.process_next("run-1", next_bar=_bar(), event_id="event-1")

    assert journal.get_checkpoint("run-1") == {}
    assert journal.list_pending_outbox("run-1") == []


def test_incomplete_bar_is_rejected_before_journal_release(tmp_path):
    coordinator, journal, sandbox = _setup(tmp_path)

    with pytest.raises(EventWorkerError, match="completed bars"):
        coordinator.process_next("run-1", next_bar=_bar(is_final=False))

    assert sandbox.requests == []
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone() == ("PAUSED",)
        assert connection.execute("SELECT COUNT(*) FROM strategy_event WHERE run_id='run-1'").fetchone() == (0,)


@pytest.mark.parametrize(
    "changes",
    [
        {"instrument_id": "MSFT"},
        {"currency": "EUR"},
        {"timezone": "UTC"},
        {"session_date": date(2026, 10, 24)},  # Saturday, not an XNYS session.
        {"start_at": NOW.replace(minute=31)},
        {"end_at": NOW.replace(minute=59)},
    ],
    ids=["wrong-instrument", "wrong-currency", "wrong-timezone", "weekend", "wrong-open", "wrong-close"],
)
def test_coordinator_pauses_before_release_when_bar_breaks_frozen_instrument_or_xnys_contract(
    tmp_path, changes,
):
    coordinator, journal, sandbox = _setup(tmp_path)
    invalid = {**_bar(), **changes}

    with pytest.raises(EventWorkerError):
        coordinator.process_next("run-1", next_bar=invalid, event_id="invalid-bar")

    assert sandbox.requests == []
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone() == ("PAUSED",)
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_event WHERE run_id='run-1'"
        ).fetchone() == (0,)


def test_coordinator_accepts_pinned_xnys_early_close_bar(tmp_path):
    from src.bt_runtime.calendars import resolve_xnys_session

    coordinator, journal, sandbox = _setup(
        tmp_path, now=lambda: datetime(2026, 11, 30, 22, tzinfo=timezone.utc),
    )
    half_day = resolve_xnys_session(date(2026, 11, 27))
    assert half_day is not None and half_day.early_close
    valid = {
        **_bar(),
        "session_date": half_day.session_date,
        "start_at": half_day.open_at,
        "end_at": half_day.close_at,
        "available_at": half_day.close_at,
    }

    outcome = coordinator.process_next("run-1", next_bar=valid, event_id="half-day-bar")

    assert outcome.committed is True
    assert outcome.event.payload["bar"]["session_date"] == "2026-11-27"
    assert len(sandbox.requests) == 1


def test_coordinator_pauses_before_release_when_bar_is_not_yet_available(tmp_path):
    from src.bt_runtime.calendars import resolve_xnys_session

    coordinator, journal, sandbox = _setup(
        tmp_path, now=lambda: datetime(2026, 10, 20, 21, tzinfo=timezone.utc),
    )
    future_session = resolve_xnys_session(date(2026, 10, 26))
    assert future_session is not None
    future = {
        **_bar(),
        "session_date": future_session.session_date,
        "start_at": future_session.open_at,
        "end_at": future_session.close_at,
        "available_at": future_session.close_at,
    }

    with pytest.raises(EventWorkerError, match="not yet available"):
        coordinator.process_next("run-1", next_bar=future, event_id="future-bar")

    assert sandbox.requests == []
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone() == ("PAUSED",)
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_event WHERE run_id='run-1'"
        ).fetchone() == (0,)


def test_coordinator_revalidates_durable_released_bar_before_worker_execution(tmp_path):
    coordinator, journal, sandbox = _setup(tmp_path)
    invalid = {**_bar(), "currency": "EUR"}
    journal.stage_next_event("run-1", {"bar": invalid}, event_id="preexisting-invalid-event")
    journal.release_next_event("run-1")

    with pytest.raises(EventWorkerError, match="USD instrument"):
        coordinator.process_next("run-1", next_bar=_bar(), event_id="ignored-replacement")

    assert sandbox.requests == []
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT state FROM execution_run WHERE run_id='run-1'"
        ).fetchone() == ("PAUSED",)
        row = connection.execute(
            "SELECT event_id, committed_at FROM strategy_event WHERE run_id='run-1'"
        ).fetchone()
    assert row == ("preexisting-invalid-event", None)


def test_paper_coordinator_rejects_source_outside_upload_policy_before_staging(tmp_path):
    source = b"import socket\ndef on_event(event, state): return {'state': {}, 'order_proposals': []}\n"
    coordinator, journal, sandbox = _setup(tmp_path, source=source)

    with pytest.raises(EventWorkerError, match="outside the supported worker API"):
        coordinator.process_next("run-1", next_bar=_bar(), event_id="event-1")

    assert sandbox.requests == []
    with pytest.raises(EventNotReady):
        journal.release_next_event("run-1")


def test_normalized_strategy_failure_keeps_only_safe_public_fields():
    class FailedSandbox:
        def run_json(self, request):
            raise SandboxStrategyFailure("strategy_runtime_error", "NameError", 3)

    with pytest.raises(EventWorkerError) as caught:
        invoke_strategy_worker(
            sandbox=FailedSandbox(), source="def on_event(event, state): pass", event_id="run:1",
            bar=Bar.model_validate(_bar()), state={},
        )
    assert caught.value.public_failure == {
        "category": "strategy_runtime_error", "error_type": "NameError", "source_line": 3,
    }
    assert "NameError" not in str(caught.value)


def test_run_version_must_resolve_to_uploaded_python(tmp_path):
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "template", "source_kind": "natural_language_template",
            "template_id": "sma_cross", "template_version": "1.0", "instrument_ids": ["us_equity_demo"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
            "parameters": {"fast_period": 2, "slow_period": 5},
            "position_policy": {"mode": "target_fraction", "allocation_fraction": "0.2"},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        }),
        template_source=b"trusted template", engine_version="bt/1.9.78.123", compiler_version="compiler/1",
    )
    journal = SQLiteEventJournal(tmp_path / "execution.sqlite")
    journal.create_run(run_id="run-template", deployment_id="deployment-1", account_ref="account",
                       environment=Environment.PAPER, strategy_version_hash=version.version_hash)
    sandbox = _RecordingSandbox()
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)

    with pytest.raises(EventWorkerError, match="Python upload versions"):
        coordinator.process_next("run-template", next_bar=_bar())

    assert sandbox.requests == []
    with pytest.raises(EventNotReady):
        journal.release_next_event("run-template")
