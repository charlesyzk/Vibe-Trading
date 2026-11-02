from datetime import date, datetime, timezone
from types import SimpleNamespace
import time

import pytest

from src.execution.order_ledger import ExecutionControlState
from src.execution.paper_daily_scheduler import PaperDailyScheduleError, PaperDailyScheduler


def utc(day, hour=15):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def bar(day, symbol="MSFT"):
    return SimpleNamespace(session_date=date(2026, 10, 30) if day == 30 else date(2026, 11, day), instrument_id=symbol)


class Manager:
    def __init__(self):
        self.state = ExecutionControlState.RUNNING
        self.bars = []
        self.reasons = []

    def status(self):
        return SimpleNamespace(control_state=self.state)

    def pause(self, scope, *, reason):
        self.reasons.append(reason)
        self.state = ExecutionControlState.PAUSED

    def push_scheduled_completed_bar(self, scope, *, expected_strategy_version_id, bar, is_current):
        assert expected_strategy_version_id == "version" and is_current()
        self.bars.append(bar)


def make_scheduler(manager, clock, source, current=lambda: True):
    return PaperDailyScheduler(manager=manager, scope="scope", version_id="version", initial_bar=bar(30),
        source=source, valid_from=utc(2, 14), valid_until=utc(6, 21), now=lambda: clock[0], is_current=current)


def test_scheduler_feeds_previous_session_once_at_next_open():
    manager = Manager()
    clock = [utc(2)]
    calls = []
    def source(*, after_session):
        calls.append(after_session)
        return (bar(2),)
    scheduler = make_scheduler(manager, clock, source)
    assert not scheduler.tick()
    clock[0] = utc(3)
    assert scheduler.tick()
    assert not scheduler.tick()
    assert calls == [date(2026, 10, 30)]
    assert manager.bars == [bar(2)]


def test_scheduler_rejects_missing_or_skipped_sessions_and_pauses():
    manager = Manager()
    clock = [utc(3)]
    scheduler = make_scheduler(manager, clock, lambda **_: ())
    with pytest.raises(PaperDailyScheduleError, match="unavailable"):
        scheduler.tick()
    assert manager.reasons == ["paper_daily_schedule_failed"]
    manager = Manager()
    clock[0] = utc(4)
    scheduler = make_scheduler(manager, clock, lambda **_: pytest.fail("must not replay stale sessions"))
    with pytest.raises(PaperDailyScheduleError, match="missed"):
        scheduler.tick()
    assert manager.state is ExecutionControlState.PAUSED


def test_scheduler_never_feeds_stopped_or_replaced_session_and_pauses_at_expiry():
    manager = Manager()
    clock = [utc(3)]
    source = lambda **_: pytest.fail("must not fetch")
    scheduler = make_scheduler(manager, clock, source, current=lambda: False)
    assert not scheduler.tick()
    scheduler = make_scheduler(manager, clock, source)
    manager.state = ExecutionControlState.PAUSED
    assert not scheduler.tick()
    manager.state = ExecutionControlState.RUNNING
    clock[0] = utc(6, 21)
    assert not scheduler.tick()
    assert manager.reasons == ["paper_entry_window_expired"]


def test_scheduler_rechecks_session_after_slow_history_fetch():
    manager = Manager()
    clock = [utc(3)]
    def source(**_):
        clock[0] = utc(3, 21)
        return (bar(2),)
    scheduler = make_scheduler(manager, clock, source)
    with pytest.raises(PaperDailyScheduleError, match="closed while loading"):
        scheduler.tick()
    assert manager.bars == []
    assert manager.state is ExecutionControlState.PAUSED


def test_background_scheduler_delivers_and_exits_after_session_pause():
    manager = Manager()
    scheduler = make_scheduler(manager, [utc(3)], lambda **_: (bar(2),))
    scheduler.start(interval_seconds=0.01)
    deadline = time.monotonic() + 2
    while not manager.bars and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.bars == [bar(2)]
    manager.state = ExecutionControlState.PAUSED
    scheduler._thread.join(timeout=2)
    assert not scheduler._thread.is_alive()


def test_old_scheduler_cannot_feed_restarted_manager_session(tmp_path):
    from src.execution.paper_execution_manager import PaperExecutionManager
    from tests.execution.test_paper_execution_manager import FakePaperRuntime, _principal, _ready, _scope

    scope, runtimes = _scope(), []
    def factory(actual_scope):
        runtime = FakePaperRuntime(actual_scope, tmp_path / "scheduled-lifecycle.sqlite")
        runtime.version_id, runtime.bars = "version", []
        runtime.push_completed_bar = runtime.bars.append
        runtime.quiescent = True
        runtimes.append(runtime)
        return runtime
    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready(),
                                    control_authorizer=lambda _principal, _scope, action: action in {"activate", "stop"})
    manager.start(scope)
    manager.activate(scope, _principal())
    token = manager.scheduled_session_token(scope)
    scheduler = PaperDailyScheduler(manager=manager, scope=scope, version_id="version", initial_bar=bar(30),
        source=lambda **_: (bar(2),), valid_from=utc(2, 14), valid_until=utc(6, 21), now=lambda: utc(3),
        is_current=lambda: manager.is_scheduled_session_current(scope, token))
    assert scheduler.tick()
    assert runtimes[0].bars == [bar(2)]
    assert manager.stop(scope, _principal())
    manager.start(scope)
    manager.activate(scope, _principal())
    assert not scheduler.tick()
    assert runtimes[1].bars == []
    assert manager.stop(scope, _principal())
