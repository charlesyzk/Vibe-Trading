from __future__ import annotations

from pathlib import Path

import pytest

from src.execution.contracts import Environment
from src.execution.execution_service import (
    ExecutionServiceNotReady,
    PaperExecutionService,
)
from src.execution.order_ledger import ExecutionControlState, SQLiteOrderLedger
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.paper_execution_manager import (
    PaperExecutionManager,
    PaperExecutionManagerError,
    PaperRuntimeAuthorizationError,
    PaperRuntimeConflict,
    PaperRuntimeScope,
    PaperRuntimeUnavailable,
    PaperRuntimeNotReady,
)
from src.session.models import AuthMethod, Principal


class FakePaperRuntime:
    def __init__(self, scope: PaperRuntimeScope, path: Path) -> None:
        self.service = PaperExecutionService(
            ledger=SQLiteOrderLedger(path),
            lock_directory=path.parent / "locks",
            broker=scope.broker,
            account_ref=scope.account_ref,
            environment=scope.environment,
            owner_id="manager-test",
            recovery_check=lambda: True,
            activation_check=lambda: True,
            quiesce_check=lambda: self.quiescent,
        )
        self.quiescent = False
        self.started = False
        self.activation_calls = 0
        self.cancelled_intents = []
        self.durable_database_paths = (self.service.ledger.path,)
        self.health_error_code = None
        self.recovery_calls = 0
        self.recovery_error = None

    def start_paused(self) -> None:
        self.started = True
        self.service.start()

    def activate(self) -> None:
        self.activation_calls += 1
        self.service.activate()

    def recover_failed_session(self) -> None:
        self.recovery_calls += 1
        if self.recovery_error is not None:
            raise self.recovery_error
        self.health_error_code = None

    def pause(self, *, reason: str) -> None:
        self.service.pause(reason=reason)

    def cancel_owned_order(self, intent_id: str):
        self.cancelled_intents.append(intent_id)
        return intent_id

    def stop(self) -> bool:
        return self.service.stop()


def _scope(account: str = "paper-fixture") -> PaperRuntimeScope:
    return PaperRuntimeScope("IBKR", account, Environment.PAPER)


def _principal(*, attributable: bool = True) -> Principal:
    return Principal(
        subject="fixture-human" if attributable else "shared-key-holder",
        auth_method=AuthMethod.FEDERATED_IDENTITY if attributable else AuthMethod.SHARED_KEY,
    )


def _ready() -> PaperExecutionReadiness:
    return PaperExecutionReadiness(
        strategy_session_builder=True,
        attributable_control_authorizer=True,
        account_and_reconciliation_facts=True,
        market_data_and_verified_contracts=True,
        risk_and_cost_facts=True,
    )


def test_missing_runtime_factory_has_no_start_side_effect(tmp_path):
    manager = PaperExecutionManager(runtime_factory=None)

    with pytest.raises(PaperRuntimeUnavailable, match="not configured"):
        manager.start(_scope())

    assert manager.status() is None
    assert manager.shutdown_active_runtime() is True
    assert manager.status() is None


def test_activity_uses_active_trusted_scope_and_marks_unproven_positions_unknown(tmp_path):
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: FakePaperRuntime(scope, tmp_path / "activity.sqlite"),
        startup_readiness=_ready(),
    )
    scope = _scope("trusted-account")
    manager.start(scope)

    snapshot = manager.activity_snapshot(limit=7)

    assert snapshot["broker"] == "IBKR"
    assert snapshot["environment"] == "paper"
    assert snapshot["history_limit"] == 7
    assert snapshot["latest_activity_seq"] == manager.latest_activity_seq()
    assert snapshot["latest_activity_seq"] > 0
    assert snapshot["orders"] == []
    assert snapshot["positions"] == {
        "source": "unknown",
        "as_of": None,
        "positions": [],
        "reason": "no fresh authoritative broker snapshot is available",
    }
    assert snapshot["risk"]["source"] == "ledger_reservations_only"
    assert snapshot["risk"]["risk_limits"] == "unavailable"
    assert "account_ref" not in snapshot
    assert manager.shutdown_active_runtime() is False
    manager._entry.runtime.quiescent = True
    assert manager.shutdown_active_runtime() is True


def test_activity_event_reader_uses_only_manager_active_scope(tmp_path):
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: FakePaperRuntime(scope, tmp_path / "activity-scope.sqlite"),
        startup_readiness=_ready(),
    )
    manager.start(_scope("trusted-account"))
    ledger = manager._entry.runtime.service.ledger
    baseline = manager.latest_activity_seq()
    ledger.set_execution_control(
        broker="IBKR", account_ref="trusted-account", environment=Environment.PAPER,
        state=ExecutionControlState.PAUSED, reason="trusted-fixture",
    )
    ledger.set_execution_control(
        broker="IBKR", account_ref="other-account", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING, reason="must-not-leak",
    )

    events = manager.activity_events_since(after_seq=baseline)

    assert manager.latest_activity_seq() == baseline + 1
    assert [event["seq"] for event in events] == [baseline + 1]
    assert events[0]["payload"] == {"state": "PAUSED", "reason": "trusted-fixture"}
    assert "other-account" not in str(events)
    manager._entry.runtime.quiescent = True
    assert manager.shutdown_active_runtime() is True


@pytest.mark.parametrize(
    ("missing", "reason_code"),
    [
        ("strategy_session_builder", "strategy_session_builder"),
        ("attributable_control_authorizer", "attributable_control_authorizer"),
        ("account_and_reconciliation_facts", "account_reconciliation_facts"),
        ("market_data_and_verified_contracts", "market_data_verified_contracts"),
        ("risk_and_cost_facts", "risk_cost_facts"),
    ],
)
def test_incomplete_readiness_blocks_factory_before_any_runtime_side_effect(tmp_path, missing, reason_code):
    calls = []
    capabilities = {
        "strategy_session_builder": True,
        "attributable_control_authorizer": True,
        "account_and_reconciliation_facts": True,
        "market_data_and_verified_contracts": True,
        "risk_and_cost_facts": True,
    }
    capabilities[missing] = False
    readiness = PaperExecutionReadiness(**capabilities)
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: calls.append(scope) or FakePaperRuntime(
            scope, tmp_path / "must-not-be-created.sqlite"
        ),
        startup_readiness=readiness,
    )

    with pytest.raises(PaperRuntimeNotReady, match="composition is incomplete") as error:
        manager.start(_scope())

    assert reason_code in str(error.value)
    assert calls == []
    assert manager.status() is None
    assert not (tmp_path / "must-not-be-created.sqlite").exists()


def test_omitted_readiness_defaults_to_fail_closed_before_factory(tmp_path):
    calls = []
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: calls.append(scope) or FakePaperRuntime(
            scope, tmp_path / "blocked.sqlite"
        ),
    )

    with pytest.raises(PaperRuntimeNotReady):
        manager.start(_scope())

    assert calls == []
    assert manager.status() is None


def test_manager_rejects_unimplemented_brokers_and_live_scope():
    with pytest.raises(ValueError, match="IBKR broker only"):
        PaperRuntimeScope("QMT", "account", Environment.PAPER)
    with pytest.raises(ValueError, match="PAPER scopes only"):
        PaperRuntimeScope("IBKR", "account", Environment.LIVE)


def test_manager_rejects_split_durable_database_paths_before_runtime_start(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "ledger.sqlite")
        runtime.durable_database_paths = (str(tmp_path / "callback.sqlite"),)
        return runtime

    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready())
    with pytest.raises(PaperExecutionManagerError, match="safe paused startup") as error:
        manager.start(_scope())

    assert "share one canonical database" in str(error.value.__cause__)
    assert runtime is not None and runtime.started is False
    assert manager.status() is None


def test_start_is_recovered_but_paused_and_manager_has_one_slot(tmp_path):
    runtimes = []

    def factory(scope):
        runtime = FakePaperRuntime(scope, tmp_path / f"{scope.account_ref}.sqlite")
        runtimes.append(runtime)
        return runtime

    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready())
    status = manager.start(_scope())

    assert status.phase == "READY"
    assert status.control_state is ExecutionControlState.PAUSED
    assert status.owner_lease_held is True
    assert runtimes[0].started is True
    assert manager.start(_scope()) == status
    with pytest.raises(PaperRuntimeConflict, match="slot is already occupied"):
        manager.start(_scope("different-paper-account"))
    assert len(runtimes) == 1


def test_activation_requires_attributable_actor_and_explicit_control_grant(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "orders.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda principal, _scope, action: (
            principal.subject == "fixture-human" and action == "activate"
        ),
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)

    with pytest.raises(PaperRuntimeAuthorizationError, match="attributable"):
        manager.activate(scope, _principal(attributable=False))
    with pytest.raises(PaperRuntimeAuthorizationError, match="does not authorize"):
        manager.activate(scope, Principal("other-human", AuthMethod.FEDERATED_IDENTITY))
    assert runtime is not None and runtime.activation_calls == 0

    status = manager.activate(scope, _principal())
    assert status.control_state is ExecutionControlState.RUNNING
    assert runtime.activation_calls == 1


def test_failed_session_recovery_uses_separate_grant_and_returns_paused(tmp_path):
    runtime = None
    actions = {"recover"}

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "recover.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda principal, _scope, action: principal.attributable and action in actions,
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)
    assert runtime is not None
    runtime.health_error_code = "managed_python_session_failed"

    with pytest.raises(PaperRuntimeAuthorizationError, match="attributable"):
        manager.recover_failed_session(scope, _principal(attributable=False))
    with pytest.raises(PaperRuntimeAuthorizationError, match="does not authorize"):
        manager.activate(scope, _principal())
    assert runtime.recovery_calls == 0
    assert runtime.activation_calls == 0

    status = manager.recover_failed_session(scope, _principal())
    assert status.phase == "READY"
    assert status.control_state is ExecutionControlState.PAUSED
    assert status.owner_lease_held is True
    assert runtime.recovery_calls == 1
    assert runtime.activation_calls == 0
    with pytest.raises(PaperRuntimeAuthorizationError, match="does not authorize"):
        manager.activate(scope, _principal())
    assert runtime.service.state()[0] is ExecutionControlState.PAUSED


def test_failed_session_recovery_failure_keeps_slot_paused_and_lease_held(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "recover-failed.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda principal, _scope, action: principal.attributable and action == "recover",
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)
    assert runtime is not None
    runtime.health_error_code = "managed_python_session_failed"
    runtime.recovery_error = RuntimeError("recovery fixture failed")

    with pytest.raises(PaperExecutionManagerError, match="remains paused"):
        manager.recover_failed_session(scope, _principal())

    status = manager.status()
    assert status is not None
    assert status.phase == "READY"
    assert status.control_state is ExecutionControlState.PAUSED
    assert status.owner_lease_held is True
    assert status.last_error_code == "RuntimeError"
    assert runtime.activation_calls == 0


def test_start_for_principal_requires_attributable_start_grant_before_factory(tmp_path):
    factory_calls = []
    actions = []
    grant = {"allowed": False}

    def factory(scope):
        factory_calls.append(scope)
        return FakePaperRuntime(scope, tmp_path / "authorized-start.sqlite")

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda principal, scope, action: (
            actions.append((principal.subject, scope, action)) is None
            and principal.subject == "fixture-human"
            and action == "start"
            and grant["allowed"]
        ),
        startup_readiness=_ready(),
    )
    scope = _scope()

    with pytest.raises(PaperRuntimeAuthorizationError, match="attributable"):
        manager.start_for_principal(scope, _principal(attributable=False))
    assert actions == []
    assert factory_calls == []
    assert manager.status() is None

    with pytest.raises(PaperRuntimeAuthorizationError, match="does not authorize"):
        manager.start_for_principal(scope, Principal("fixture-human", AuthMethod.FEDERATED_IDENTITY))
    assert actions == [("fixture-human", scope, "start")]
    assert factory_calls == []

    grant["allowed"] = True
    status = manager.start_for_principal(scope, _principal())
    assert status.phase == "READY"
    assert status.control_state is ExecutionControlState.PAUSED
    assert factory_calls == [scope]


def test_manager_cancel_has_separate_attributable_cancel_grant(tmp_path):
    runtime = None
    actions = []

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "cancel-orders.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda principal, _scope, action: (
            actions.append((principal.subject, action)) is None
            and principal.subject == "fixture-human"
            and action == "activate"
        ),
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)

    with pytest.raises(PaperRuntimeAuthorizationError, match="attributable"):
        manager.cancel_owned_order(scope, "intent-1", _principal(attributable=False))
    with pytest.raises(PaperRuntimeAuthorizationError, match="does not authorize"):
        manager.cancel_owned_order(scope, "intent-1", _principal())
    assert runtime is not None and runtime.cancelled_intents == []

    # Cancellation is an independent grant; it does not need strategy activate.
    manager.control_authorizer = lambda principal, _scope, action: (
        principal.subject == "fixture-human" and action == "cancel"
    )
    assert manager.cancel_owned_order(scope, "intent-1", _principal()) == "intent-1"
    assert runtime.cancelled_intents == ["intent-1"]


def test_manager_status_surfaces_stable_runtime_health_code(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "session-health.sqlite")
        return runtime

    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready())
    manager.start(_scope())
    assert runtime is not None
    runtime.health_error_code = "managed_python_session_failed"

    status = manager.status()
    assert status is not None
    assert status.last_error_code == "managed_python_session_failed"


def test_pause_blocks_dispatch_and_stop_retains_slot_until_quiescent(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "orders.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda _principal, _scope, action: action in {"activate", "stop"},
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)
    manager.activate(scope, _principal())

    paused = manager.pause(scope, reason="worker_timeout")
    assert paused.control_state is ExecutionControlState.PAUSED
    assert runtime.service.ledger.get_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER
    )[0] is ExecutionControlState.PAUSED

    assert manager.stop(scope, _principal()) is False
    retained = manager.status()
    assert retained is not None
    assert retained.phase == "STOPPING"
    assert retained.control_state is ExecutionControlState.STOPPING
    assert retained.owner_lease_held is True

    with pytest.raises(PaperRuntimeConflict, match="slot is already occupied"):
        manager.start(_scope("different-paper-account"))

    runtime.quiescent = True
    assert manager.stop(scope, _principal()) is True
    assert manager.status() is None


def test_app_shutdown_retries_until_runtime_proves_quiescence(tmp_path):
    runtime = None
    factory_calls = []

    def factory(scope):
        nonlocal runtime
        factory_calls.append(scope.key)
        runtime = FakePaperRuntime(scope, tmp_path / "shutdown-orders.sqlite")
        return runtime

    manager = PaperExecutionManager(
        runtime_factory=factory,
        control_authorizer=lambda _principal, _scope, action: action == "activate",
        startup_readiness=_ready(),
    )
    scope = _scope()
    manager.start(scope)
    assert runtime is not None and runtime.service.lease_held
    manager.activate(scope, _principal())
    assert runtime.service.state()[0] is ExecutionControlState.RUNNING

    assert manager.shutdown_active_runtime() is False
    retained = manager.status()
    assert retained is not None
    assert retained.phase == "STOPPING"
    assert retained.control_state is ExecutionControlState.STOPPING
    assert retained.owner_lease_held is True

    runtime.quiescent = True
    assert manager.shutdown_active_runtime() is True
    assert manager.status() is None
    assert runtime.service.state()[0] is ExecutionControlState.STOPPED
    assert runtime.service.lease_held is False
    assert factory_calls == [scope.key]


def test_app_shutdown_records_runtime_exception_and_keeps_owned_entry(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "shutdown-error.sqlite")
        return runtime

    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready())
    manager.start(_scope())
    assert runtime is not None

    def fail_stop():
        raise TimeoutError("sensitive transport detail must not be surfaced")

    runtime.stop = fail_stop
    assert manager.shutdown_active_runtime() is False
    status = manager.status()
    assert status is not None
    assert status.phase == "ERROR"
    assert status.last_error_code == "TimeoutError"
    assert status.owner_lease_held is True
    assert manager.shutdown_active_runtime() is False


def test_failed_recovery_retains_runtime_and_owner_for_review(tmp_path):
    runtime = None

    def factory(scope):
        nonlocal runtime
        runtime = FakePaperRuntime(scope, tmp_path / "orders.sqlite")
        runtime.service.recovery_check = lambda: False
        return runtime

    manager = PaperExecutionManager(runtime_factory=factory, startup_readiness=_ready())

    with pytest.raises(PaperExecutionManagerError, match="safe paused startup"):
        manager.start(_scope())

    status = manager.status()
    assert status is not None
    assert status.phase == "START_FAILED"
    assert status.control_state is ExecutionControlState.RECOVERING
    assert status.owner_lease_held is True
    with pytest.raises(PaperRuntimeConflict, match="slot is already occupied"):
        manager.start(_scope("other"))

    runtime.service.recovery_check = lambda: True
    recovered = manager.retry_recovery(_scope())
    assert recovered.phase == "READY"
    assert recovered.control_state is ExecutionControlState.PAUSED
    assert recovered.owner_lease_held is True
