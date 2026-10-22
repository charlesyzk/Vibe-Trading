from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace

import backtrader as bt
import pytest

from src.bt_runtime.backtest_event_journal import (
    BacktestJournalError,
    BacktestRunMismatch,
    SQLiteBacktestEventJournal,
)
from src.execution.contracts import Bar
from src.execution.event_worker import EventWorkerError, PythonBacktestEventCoordinator


class RecordingSandbox:
    def __init__(self, *, fail: bool = False):
        self.calls = []
        self.fail = fail

    def run_json(self, request):
        self.calls.append(request)
        if self.fail:
            self.fail = False
            raise RuntimeError("temporary worker crash")
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"seen": request["state"].get("seen", 0) + 1},
            "order_proposals": [{"side": "BUY", "integer_qty": 1, "reason": "test"}],
        })


def _journal(path, *, version="a" * 64, spec="b" * 64, config="c" * 64):
    journal = SQLiteBacktestEventJournal(path)
    journal.bind_run(
        run_id="bt-run",
        strategy_version_hash=version,
        strategy_spec_hash=spec,
        run_config_hash=config,
    )
    return journal


def _bar(*, close: str = "12.50") -> Bar:
    return Bar.model_validate({
        "instrument_id": "us_equity_demo",
        "session_date": "2026-10-20",
        "start_at": "2026-10-20T13:30:00Z",
        "end_at": "2026-10-20T20:00:00Z",
        "available_at": "2026-10-20T20:00:00Z",
        "timezone": "America/New_York",
        "open": "12.00", "high": str(max(13.0, float(close) + 0.5)), "low": "11.50", "close": close, "volume": "1000",
        "currency": "USD", "is_final": True, "source": "synthetic_fixture", "adjustment": "unadjusted",
        "quality_flags": [],
    })


def _next_bar() -> Bar:
    return Bar.model_validate({
        "instrument_id": "us_equity_demo",
        "session_date": "2026-10-21",
        "start_at": "2026-10-21T13:30:00Z",
        "end_at": "2026-10-21T20:00:00Z",
        "available_at": "2026-10-21T20:00:00Z",
        "timezone": "America/New_York",
        "open": "12.50", "high": "13.00", "low": "12.00", "close": "12.75", "volume": "1200",
        "currency": "USD", "is_final": True, "source": "synthetic_fixture", "adjustment": "unadjusted",
        "quality_flags": [],
    })


def test_committed_result_is_reused_after_reopen_without_an_account_or_dispatch_surface(tmp_path):
    path = tmp_path / "backtest.sqlite3"
    journal = _journal(path)
    sandbox = RecordingSandbox()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox, released_bars=(_bar(),),
    )
    first = coordinator.process_event(1, feed_available_at=_bar().available_at)
    assert first.order_proposals[0].integer_qty == 1
    assert len(sandbox.calls) == 1

    reopened = SQLiteBacktestEventJournal(path)
    retry_sandbox = RecordingSandbox()
    retry = PythonBacktestEventCoordinator(
        journal=reopened, run_id="bt-run", source="source", sandbox=retry_sandbox, released_bars=(_bar(),),
    )
    result = retry.process_event(1, feed_available_at=_bar().available_at)

    assert result == first
    assert retry_sandbox.calls == []
    assert reopened.get_status("bt-run")["next_seq"] == 2
    with __import__("sqlite3").connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "order_outbox" not in tables
    assert "execution_run" not in tables
    connection = reopened._connect()
    try:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.parametrize("checkpoint_json", ['{"seen":999}', '{"seen":'])
def test_checkpoint_mismatch_or_malformed_json_pauses_before_worker_on_restart(tmp_path, checkpoint_json):
    path = tmp_path / "checkpoint-integrity.sqlite3"
    journal = _journal(path)
    initial_sandbox = RecordingSandbox()
    first = _bar()
    next_bar = _next_bar()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=initial_sandbox,
        released_bars=(first, next_bar),
    )
    coordinator.process_event(1, feed_available_at=first.available_at)
    assert len(initial_sandbox.calls) == 1

    with __import__("sqlite3").connect(path) as connection:
        connection.execute(
            "UPDATE backtest_run SET checkpoint_json=? WHERE run_id='bt-run'", (checkpoint_json,)
        )

    reopened = SQLiteBacktestEventJournal(path)
    retry_sandbox = RecordingSandbox()
    restarted = PythonBacktestEventCoordinator(
        journal=reopened, run_id="bt-run", source="source", sandbox=retry_sandbox,
        released_bars=(first, next_bar),
    )
    with pytest.raises(BacktestJournalError, match="checkpoint integrity check failed"):
        restarted.process_event(2, feed_available_at=next_bar.available_at)

    assert retry_sandbox.calls == []
    assert reopened.get_status("bt-run")["state"] == "PAUSED"
    assert reopened.get_status("bt-run")["next_seq"] == 2
    with __import__("sqlite3").connect(path) as connection:
        assert connection.execute(
            "SELECT seq FROM backtest_event WHERE run_id='bt-run' ORDER BY seq"
        ).fetchall() == [(1,)]


def test_checkpoint_is_revalidated_before_retrying_an_uncommitted_event(tmp_path):
    path = tmp_path / "checkpoint-retry.sqlite3"
    journal = _journal(path)
    first, next_bar = _bar(), _next_bar()
    first_sandbox = RecordingSandbox()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=first_sandbox,
        released_bars=(first, next_bar),
    )
    coordinator.process_event(1, feed_available_at=first.available_at)
    retrying_sandbox = RecordingSandbox(fail=True)
    retrying_coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=retrying_sandbox,
        released_bars=(first, next_bar),
    )
    with pytest.raises(RuntimeError, match="temporary worker crash"):
        retrying_coordinator.process_event(2, feed_available_at=next_bar.available_at)
    assert len(retrying_sandbox.calls) == 1

    with __import__("sqlite3").connect(path) as connection:
        connection.execute(
            "UPDATE backtest_run SET checkpoint_json=? WHERE run_id='bt-run'", ('{"seen":77}',)
        )

    reopened = SQLiteBacktestEventJournal(path)
    retry_sandbox = RecordingSandbox()
    restarted = PythonBacktestEventCoordinator(
        journal=reopened, run_id="bt-run", source="source", sandbox=retry_sandbox,
        released_bars=(first, next_bar),
    )
    with pytest.raises(BacktestJournalError, match="checkpoint integrity check failed"):
        restarted.process_event(2, feed_available_at=next_bar.available_at)
    assert retry_sandbox.calls == []
    assert reopened.get_status("bt-run")["state"] == "PAUSED"


def test_committed_event_replay_still_validates_latest_checkpoint(tmp_path):
    path = tmp_path / "checkpoint-replay-integrity.sqlite3"
    journal = _journal(path)
    first = _bar()
    initial_sandbox = RecordingSandbox()
    PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=initial_sandbox,
        released_bars=(first,),
    ).process_event(1, feed_available_at=first.available_at)

    with __import__("sqlite3").connect(path) as connection:
        connection.execute(
            "UPDATE backtest_run SET checkpoint_json=? WHERE run_id='bt-run'", ('{"seen":1.0}',)
        )

    reopened = SQLiteBacktestEventJournal(path)
    retry_sandbox = RecordingSandbox()
    restarted = PythonBacktestEventCoordinator(
        journal=reopened, run_id="bt-run", source="source", sandbox=retry_sandbox,
        released_bars=(first,),
    )
    with pytest.raises(BacktestJournalError, match="checkpoint integrity check failed"):
        restarted.process_event(1, feed_available_at=first.available_at)
    assert retry_sandbox.calls == []
    assert reopened.get_status("bt-run")["state"] == "PAUSED"


def test_uncommitted_event_retries_same_bar_and_rejects_replacement(tmp_path):
    journal = _journal(tmp_path / "backtest.sqlite3")
    failed = RecordingSandbox(fail=True)
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=failed, released_bars=(_bar(),),
    )
    with pytest.raises(RuntimeError, match="temporary worker crash"):
        coordinator.process_event(1, feed_available_at=_bar().available_at)

    retry = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=RecordingSandbox(), released_bars=(_bar(),),
    )
    result = retry.process_event(1, feed_available_at=_bar().available_at)
    assert result.state == {"seen": 1}
    assert journal.get_status("bt-run")["next_seq"] == 2

    mismatch_journal = _journal(tmp_path / "mismatch.sqlite3")
    with pytest.raises(RuntimeError, match="temporary worker crash"):
        PythonBacktestEventCoordinator(
            journal=mismatch_journal, run_id="bt-run", source="source", sandbox=RecordingSandbox(fail=True),
            released_bars=(_bar(),),
        ).process_event(1, feed_available_at=_bar().available_at)
    with pytest.raises(BacktestRunMismatch, match="bar identity or content changed"):
        PythonBacktestEventCoordinator(
            journal=mismatch_journal, run_id="bt-run", source="source", sandbox=RecordingSandbox(),
            released_bars=(_bar(close="99.00"),),
        ).process_event(1, feed_available_at=_bar().available_at)


def test_run_config_or_version_mismatch_pauses_existing_backtest(tmp_path):
    path = tmp_path / "backtest.sqlite3"
    journal = _journal(path)
    with pytest.raises(BacktestRunMismatch, match="configuration changed"):
        journal.bind_run(
            run_id="bt-run",
            strategy_version_hash="a" * 64,
            strategy_spec_hash="b" * 64,
            run_config_hash="d" * 64,
        )
    assert SQLiteBacktestEventJournal(path).get_status("bt-run")["state"] == "PAUSED"


def test_backtest_coordinator_releases_only_its_immutable_next_bar(tmp_path):
    journal = _journal(tmp_path / "cursor.sqlite3")
    sandbox = RecordingSandbox()
    bars = (_bar(), _next_bar())
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox, released_bars=bars,
    )

    # A future seq with its matching future timestamp passes the coordinator's
    # per-bar check, then the durable journal detects the skipped cursor and
    # pauses before release. Even the correct current event cannot continue.
    with pytest.raises(BacktestJournalError, match="active backtest cursor"):
        coordinator.process_event(2, feed_available_at=_next_bar().available_at)
    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1
    assert journal.get_status("bt-run")["state"] == "PAUSED"
    with pytest.raises(BacktestJournalError, match="paused"):
        coordinator.process_event(1, feed_available_at=_bar().available_at)
    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1
    with pytest.raises(TypeError):
        coordinator.process_event(1, _bar().available_at, _next_bar())

    journal = _journal(tmp_path / "valid-cursor.sqlite3")
    sandbox = RecordingSandbox()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox, released_bars=bars,
    )
    coordinator.process_event(1, feed_available_at=_bar().available_at)
    coordinator.process_event(2, feed_available_at=_next_bar().available_at)
    assert [call["event"]["bar"]["session_date"] for call in sandbox.calls] == [
        "2026-10-20", "2026-10-21",
    ]
    with pytest.raises(EventWorkerError, match="outside the verified backtest bar range"):
        coordinator.process_event(3, feed_available_at=_next_bar().available_at + timedelta(days=1))


@pytest.mark.parametrize("invalid_seq", [0, -1, 3, True, 1.0, None])
def test_backtest_coordinator_pauses_on_invalid_host_cursor(tmp_path, invalid_seq):
    journal = _journal(tmp_path / f"invalid-cursor-{repr(invalid_seq)}.sqlite3")
    sandbox = RecordingSandbox()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox,
        released_bars=(_bar(), _next_bar()),
    )

    with pytest.raises(EventWorkerError, match="outside the verified backtest bar range"):
        coordinator.process_event(invalid_seq, feed_available_at=_bar().available_at)

    status = journal.get_status("bt-run")
    assert sandbox.calls == []
    assert status["next_seq"] == 1
    assert status["state"] == "PAUSED"

    # A subsequent valid-looking cursor cannot resume a run after integrity
    # failure; the pause is persisted by the journal, not held in this object.
    with pytest.raises(BacktestJournalError, match="paused"):
        coordinator.process_event(1, feed_available_at=_bar().available_at)
    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1


def test_backtest_coordinator_pauses_before_worker_when_feed_timestamp_is_misaligned(tmp_path):
    journal = _journal(tmp_path / "feed-mismatch.sqlite3")
    sandbox = RecordingSandbox()
    expected_bar = _bar()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox, released_bars=(expected_bar,),
    )

    with pytest.raises(EventWorkerError, match="does not match the verified bar cursor"):
        coordinator.process_event(
            1,
            feed_available_at=expected_bar.available_at + timedelta(days=1),
        )
    assert sandbox.calls == []
    assert journal.get_status("bt-run")["state"] == "PAUSED"
    assert journal.get_status("bt-run")["next_seq"] == 1

    naive_journal = _journal(tmp_path / "naive-feed-timestamp.sqlite3")
    naive_sandbox = RecordingSandbox()
    naive_coordinator = PythonBacktestEventCoordinator(
        journal=naive_journal,
        run_id="bt-run",
        source="source",
        sandbox=naive_sandbox,
        released_bars=(expected_bar,),
    )
    with pytest.raises(EventWorkerError, match="timezone-aware"):
        naive_coordinator.process_event(
            1,
            feed_available_at=expected_bar.available_at.replace(tzinfo=None),
        )
    assert naive_sandbox.calls == []
    assert naive_journal.get_status("bt-run")["state"] == "PAUSED"


@pytest.mark.parametrize("invalid_timestamp", [None, "2026-10-20T20:00:00Z", object()])
def test_backtest_coordinator_persists_pause_for_non_datetime_feed_timestamp(tmp_path, invalid_timestamp):
    journal = _journal(tmp_path / f"invalid-feed-timestamp-{type(invalid_timestamp).__name__}.sqlite3")
    sandbox = RecordingSandbox()
    expected_bar = _bar()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal,
        run_id="bt-run",
        source="source",
        sandbox=sandbox,
        released_bars=(expected_bar,),
    )

    with pytest.raises(EventWorkerError, match="timezone-aware datetime"):
        coordinator.process_event(1, feed_available_at=invalid_timestamp)

    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1
    assert journal.get_status("bt-run")["state"] == "PAUSED"
    with pytest.raises(BacktestJournalError, match="paused"):
        coordinator.process_event(1, feed_available_at=expected_bar.available_at)
    assert sandbox.calls == []


@pytest.mark.parametrize(
    "normalized_result",
    [
        datetime(2026, 10, 20, 20),
        object(),
    ],
    ids=["naive-datetime", "non-datetime"],
)
def test_backtest_coordinator_rejects_datetime_subclass_with_overridden_normalizer(
    tmp_path, normalized_result,
):
    method_calls = []

    class OverriddenDateTime(datetime):
        def astimezone(self, tz=None):
            method_calls.append(True)
            return normalized_result

    journal = _journal(tmp_path / "datetime-subclass.sqlite3")
    sandbox = RecordingSandbox()
    expected_bar = _bar()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal,
        run_id="bt-run",
        source="source",
        sandbox=sandbox,
        released_bars=(expected_bar,),
    )
    forged_timestamp = OverriddenDateTime(2026, 10, 20, 20, tzinfo=timezone.utc)

    with pytest.raises(EventWorkerError, match="timezone-aware datetime"):
        coordinator.process_event(1, feed_available_at=forged_timestamp)

    assert method_calls == []
    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1
    assert journal.get_status("bt-run")["state"] == "PAUSED"
    with pytest.raises(BacktestJournalError, match="paused"):
        coordinator.process_event(1, feed_available_at=expected_bar.available_at)
    assert sandbox.calls == []


def test_backtest_coordinator_persists_pause_when_feed_timezone_raises(tmp_path):
    class BrokenTimezone(tzinfo):
        def utcoffset(self, _dt):
            raise RuntimeError("malformed feed timezone")

    journal = _journal(tmp_path / "broken-feed-timezone.sqlite3")
    sandbox = RecordingSandbox()
    expected_bar = _bar()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal,
        run_id="bt-run",
        source="source",
        sandbox=sandbox,
        released_bars=(expected_bar,),
    )

    with pytest.raises(EventWorkerError, match="could not be normalized"):
        coordinator.process_event(
            1,
            feed_available_at=datetime(2024, 1, 2, 20, tzinfo=BrokenTimezone()),
        )

    assert sandbox.calls == []
    assert journal.get_status("bt-run")["next_seq"] == 1
    assert journal.get_status("bt-run")["state"] == "PAUSED"
    with pytest.raises(BacktestJournalError, match="paused"):
        coordinator.process_event(1, feed_available_at=expected_bar.available_at)
    assert sandbox.calls == []


def test_backtest_coordinator_accepts_backtrader_utc_float_rounding(tmp_path):
    source_bar = _bar()
    precise_bar = source_bar.model_copy(update={
        "available_at": source_bar.available_at.replace(microsecond=123456),
    })
    feed_timestamp = bt.num2date(bt.date2num(precise_bar.available_at), tz=timezone.utc).replace(
        tzinfo=timezone.utc
    )
    journal = _journal(tmp_path / "float-rounding.sqlite3")
    sandbox = RecordingSandbox()
    coordinator = PythonBacktestEventCoordinator(
        journal=journal, run_id="bt-run", source="source", sandbox=sandbox, released_bars=(precise_bar,),
    )

    coordinator.process_event(1, feed_available_at=feed_timestamp)
    assert len(sandbox.calls) == 1
