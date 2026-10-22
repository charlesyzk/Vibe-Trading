from __future__ import annotations

import pytest

from src.execution.contracts import Environment
from src.execution.execution_service import ExecutionServiceNotReady, PaperExecutionService
from src.execution.order_ledger import ExecutionControlState, SQLiteOrderLedger
from src.execution.owner_lease import ExecutionOwnerUnavailable


def _service(tmp_path, *, owner_id="owner-1", recovery=lambda: True, readiness=lambda: True, quiesce=lambda: True):
    return PaperExecutionService(
        ledger=SQLiteOrderLedger(tmp_path / "orders.sqlite"),
        lock_directory=tmp_path / "locks",
        broker="ibkr",
        account_ref="DU123456",
        environment=Environment.PAPER,
        owner_id=owner_id,
        recovery_check=recovery,
        activation_check=readiness,
        quiesce_check=quiesce,
    )


def test_start_recovers_to_paused_then_requires_explicit_ready_activation(tmp_path):
    checks = {"ready": False}
    service = _service(tmp_path, readiness=lambda: checks["ready"])

    assert service.state() == (ExecutionControlState.PAUSED, "not_activated")
    assert service.start() is ExecutionControlState.PAUSED
    assert service.lease_held
    assert service.state() == (
        ExecutionControlState.PAUSED,
        "recovered_requires_explicit_activation",
    )
    with pytest.raises(ExecutionServiceNotReady, match="readiness checks are incomplete"):
        service.activate()
    assert service.state()[0] is ExecutionControlState.PAUSED

    checks["ready"] = True
    assert service.activate() is ExecutionControlState.RUNNING
    assert service.state() == (ExecutionControlState.RUNNING, "explicit_activation")
    assert service.require_owner_lease().held
    service.close()
    with pytest.raises(ExecutionServiceNotReady, match="lease is not held"):
        service.require_owner_lease()


def test_runtime_can_hold_recovery_gate_while_sdk_and_spool_start(tmp_path):
    service = _service(tmp_path)

    assert service.begin_recovery() is ExecutionControlState.RECOVERING
    assert service.lease_held
    assert service.state() == (ExecutionControlState.RECOVERING, "service_start_recovery")
    assert service.complete_recovery() is ExecutionControlState.PAUSED
    assert service.state() == (ExecutionControlState.PAUSED, "recovered_requires_explicit_activation")
    assert service.stop()


def test_start_from_running_closes_gate_and_requires_fresh_recovery(tmp_path):
    checks = []
    service = _service(tmp_path, recovery=lambda: checks.append("recovered") or True)

    assert service.start() is ExecutionControlState.PAUSED
    assert service.activate() is ExecutionControlState.RUNNING
    assert service.state()[0] is ExecutionControlState.RUNNING

    assert service.start() is ExecutionControlState.PAUSED
    assert checks == ["recovered", "recovered"]
    assert service.state() == (
        ExecutionControlState.PAUSED,
        "recovered_requires_explicit_activation",
    )
    assert service.lease_held
    service.close()


def test_incomplete_recovery_holds_lease_and_prevents_second_owner(tmp_path):
    checks = {"recovered": False}
    service = _service(tmp_path, recovery=lambda: checks["recovered"])
    second = _service(tmp_path, owner_id="owner-2")

    with pytest.raises(ExecutionServiceNotReady, match="recovery is incomplete"):
        service.start()
    assert service.state() == (ExecutionControlState.RECOVERING, "service_start_recovery")
    assert service.lease_held
    with pytest.raises(ExecutionOwnerUnavailable):
        second.start()

    checks["recovered"] = True
    assert service.start() is ExecutionControlState.PAUSED
    service.close()


def test_stop_keeps_gate_and_lease_until_quiescent(tmp_path):
    checks = {"quiescent": False}
    service = _service(tmp_path, quiesce=lambda: checks["quiescent"])
    assert service.start() is ExecutionControlState.PAUSED
    assert service.activate() is ExecutionControlState.RUNNING

    assert service.stop() is False
    assert service.state() == (ExecutionControlState.STOPPING, "awaiting_quiescence")
    assert service.lease_held
    with pytest.raises(ExecutionServiceNotReady, match="cannot activate"):
        service.activate()

    checks["quiescent"] = True
    assert service.stop() is True
    assert service.state() == (ExecutionControlState.STOPPED, "quiescent")
    assert not service.lease_held
    assert service.stop() is True


def test_scope_is_paper_only_and_activation_requires_held_lease(tmp_path):
    with pytest.raises(ValueError, match="only accepts the PAPER"):
        PaperExecutionService(
            ledger=SQLiteOrderLedger(tmp_path / "orders.sqlite"),
            lock_directory=tmp_path / "locks",
            broker="ibkr",
            account_ref="DU123456",
            environment=Environment.LIVE,
            owner_id="owner-1",
            recovery_check=lambda: True,
            activation_check=lambda: True,
            quiesce_check=lambda: True,
        )

    service = _service(tmp_path)
    with pytest.raises(ExecutionServiceNotReady, match="lease is not held"):
        service.activate()


def test_graceful_close_stops_before_releasing_owner_lease(tmp_path):
    service = _service(tmp_path)
    service.start()
    service.activate()

    service.close()

    assert service.state() == (ExecutionControlState.STOPPED, "quiescent")
    assert not service.lease_held


def test_restart_recovers_a_scope_left_stopping_after_host_exit(tmp_path):
    first = _service(tmp_path, quiesce=lambda: False)
    first.start()
    first.activate()
    assert first.stop() is False
    # Simulate process death: durable STOPPING survives, the OS releases flock.
    first._lease.close()
    first._lease = None

    second = _service(tmp_path, owner_id="owner-after-restart")
    assert second.start() is ExecutionControlState.PAUSED
    assert second.state() == (
        ExecutionControlState.PAUSED,
        "recovered_requires_explicit_activation",
    )
    second.close()


def test_close_keeps_stopping_scope_and_lease_until_quiescent(tmp_path):
    checks = {"quiescent": False}
    service = _service(tmp_path, quiesce=lambda: checks["quiescent"])
    service.start()
    service.activate()

    with pytest.raises(ExecutionServiceNotReady, match="owner lease are retained"):
        service.close()
    assert service.state() == (ExecutionControlState.STOPPING, "awaiting_quiescence")
    assert service.lease_held

    checks["quiescent"] = True
    assert service.stop() is True
    assert not service.lease_held
