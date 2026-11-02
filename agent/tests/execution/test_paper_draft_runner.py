from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.execution.paper_draft_runner import PaperDraftRunner
from src.execution.paper_strategy_warmup import PaperWarmup
from src.execution.order_ledger import ExecutionControlState
from src.session.models import AuthMethod, Principal
from tests.execution.test_outbox_dispatch import _bar


def _recent_bar():
    return _bar().model_copy(update={"session_date": datetime(2026, 10, 30).date(),
        "start_at": datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc),
        "end_at": datetime(2026, 10, 30, 20, tzinfo=timezone.utc),
        "available_at": datetime(2026, 10, 30, 20, tzinfo=timezone.utc)})


def test_draft_runner_checks_latest_bar_and_window_before_start(tmp_path, monkeypatch):
    bar = _recent_bar()
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    monkeypatch.setattr("src.execution.paper_draft_runner.load_paper_order_draft", lambda **_: draft)
    schedulers = []
    class Scheduler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            schedulers.append(self)
        def start(self):
            self.started = True
    monkeypatch.setattr("src.execution.paper_draft_runner.PaperDailyScheduler", Scheduler)
    calls = []
    manager = SimpleNamespace(start_activate_and_push_completed_bar=lambda *args, **kwargs:
                              (calls.append((args, kwargs)) or SimpleNamespace(phase="READY")),
                              scheduled_session_token=lambda _: "token",
                              is_scheduled_session_current=lambda _scope, token: token == "token")
    now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    runner = PaperDraftRunner(manager=manager, scope=SimpleNamespace(account_ref="DU123"), runtime_root=tmp_path,
                              draft=draft, warmup=PaperWarmup({}, bar, "b" * 64), source=lambda: (bar,), now=lambda: now)
    principal = Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN)
    result = runner(principal, is_current=lambda: True)
    assert result["signal_enqueued"] and not result["broker_order_confirmed"]
    assert schedulers[0].started and schedulers[0].kwargs["initial_bar"] == bar
    assert calls[0][1]["bar"] == bar
    with pytest.raises(RuntimeError, match="deployment changed"):
        runner(principal, is_current=lambda: False)
    runner.source = lambda: (bar.model_copy(update={"close": bar.close + 1}),)
    with pytest.raises(ValueError, match="differs"):
        runner(principal, is_current=lambda: True)
    stale = bar.model_copy(update={"session_date": datetime(2026, 10, 29).date()})
    runner.warmup = PaperWarmup({}, stale, "b" * 64)
    runner.source = lambda: (stale,)
    with pytest.raises(ValueError, match="latest completed market session"):
        runner(principal, is_current=lambda: True)
    assert len(calls) == 1


def test_draft_runner_rechecks_window_after_history_fetch(tmp_path, monkeypatch):
    bar = _recent_bar()
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    monkeypatch.setattr("src.execution.paper_draft_runner.load_paper_order_draft", lambda **_: draft)
    clock = iter([datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc), datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc), datetime(2026, 11, 2, 21, tzinfo=timezone.utc)])
    runner = PaperDraftRunner(manager=SimpleNamespace(), scope=SimpleNamespace(account_ref="DU123"), runtime_root=tmp_path,
                              draft=draft, warmup=PaperWarmup({}, bar, "b" * 64), source=lambda: (bar,), now=lambda: next(clock))
    principal = Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN)
    with pytest.raises(ValueError, match="window is closed"):
        runner(principal, is_current=lambda: True)


def test_draft_runner_rejects_changed_draft_after_history_fetch(tmp_path, monkeypatch):
    bar = _recent_bar()
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    reads = iter((draft, {**draft, "strategy_version_id": "c" * 64}))
    monkeypatch.setattr("src.execution.paper_draft_runner.load_paper_order_draft", lambda **_: next(reads))
    runner = PaperDraftRunner(manager=SimpleNamespace(), scope=SimpleNamespace(account_ref="DU123"), runtime_root=tmp_path,
                              draft=draft, warmup=PaperWarmup({}, bar, "b" * 64), source=lambda: (bar,),
                              now=lambda: datetime(2026, 11, 2, 15, tzinfo=timezone.utc))
    principal = Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN)
    with pytest.raises(ValueError, match="draft changed"):
        runner(principal, is_current=lambda: True)


def test_multi_session_draft_does_not_start_during_weekend(tmp_path, monkeypatch):
    bar = _recent_bar()
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-10T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    monkeypatch.setattr("src.execution.paper_draft_runner.load_paper_order_draft", lambda **_: draft)
    runner = PaperDraftRunner(manager=SimpleNamespace(), scope=SimpleNamespace(account_ref="DU123"), runtime_root=tmp_path,
        draft=draft, warmup=PaperWarmup({}, bar, "b" * 64), source=lambda: pytest.fail("must not fetch"),
        now=lambda: datetime(2026, 11, 7, 15, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="regular trading hours"):
        runner(Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN), is_current=lambda: True)


def test_multi_session_runner_schedules_following_day_without_duplicate(tmp_path, monkeypatch):
    first = _recent_bar()
    monday = first.model_copy(update={"session_date": datetime(2026, 11, 2).date(),
        "start_at": datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc),
        "end_at": datetime(2026, 11, 2, 21, tzinfo=timezone.utc),
        "available_at": datetime(2026, 11, 2, 21, tzinfo=timezone.utc)})
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-06T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    monkeypatch.setattr("src.execution.paper_draft_runner.load_paper_order_draft", lambda **_: draft)
    class Manager:
        def __init__(self): self.bars = []
        def start_activate_and_push_completed_bar(self, _scope, _principal, **kwargs):
            self.bars.append(kwargs["bar"])
            return SimpleNamespace(phase="READY")
        def status(self): return SimpleNamespace(control_state=ExecutionControlState.RUNNING)
        def scheduled_session_token(self, _scope): return self
        def is_scheduled_session_current(self, _scope, token): return token is self
        def push_scheduled_completed_bar(self, _scope, **kwargs): self.bars.append(kwargs["bar"])
        def pause(self, *_args, **_kwargs): pytest.fail("unexpected pause")
    manager, clock = Manager(), [datetime(2026, 11, 2, 15, tzinfo=timezone.utc)]
    def source(*, after_session=None): return (first,) if after_session is None else (monday,)
    runner = PaperDraftRunner(manager=manager, scope=SimpleNamespace(account_ref="DU123"), runtime_root=tmp_path,
        draft=draft, warmup=PaperWarmup({}, first, "b" * 64), source=source, now=lambda: clock[0])
    runner(Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN), is_current=lambda: True)
    try:
        clock[0] = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
        assert runner._scheduler.tick()
        assert not runner._scheduler.tick()
        assert manager.bars == [first, monday]
    finally:
        runner._scheduler.stop()
