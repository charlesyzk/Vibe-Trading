from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Environment
from src.execution.dispatch_service import PaperOrderDispatchService
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import PythonEventCoordinator
from src.execution.managed_python_paper_deployment import (
    ManagedPythonPaperDeploymentError,
    ManagedPythonPaperDeploymentFactory,
)
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.order_ledger import ExecutionControlState
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.outbox_dispatch import PythonOutboxIntentHandoff
from src.execution.paper_execution_manager import (
    PaperExecutionManager,
    PaperExecutionManagerError,
    PaperRuntimeScope,
)
from src.execution.versions import StrategyVersionStore
from src.session.models import AuthMethod, Principal
from tests.execution.test_managed_python_paper_session import (
    NoProposalSandbox,
    _make_runtime,
    _spec,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def _scope(account_ref: str = "paper-session") -> PaperRuntimeScope:
    return PaperRuntimeScope("IBKR", account_ref, Environment.PAPER)


def _session_builder(tmp_path: Path, *, separate_journal: bool = False):
    def build(runtime, scope):
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            _spec(), upload_source=b"def on_event(event, state): return {}\n",
            engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
        )
        journal_path = tmp_path / "other-journal.sqlite" if separate_journal else runtime.ledger.path
        journal = SQLiteEventJournal(journal_path)
        journal.create_run(
            run_id="deployment-run", deployment_id="deployment-1",
            account_ref=scope.account_ref, environment=Environment.PAPER,
            strategy_version_hash=version.version_hash,
        )
        coordinator = PythonEventCoordinator(
            journal=journal, versions=versions, sandbox=NoProposalSandbox(),
        )
        handoff = PythonOutboxIntentHandoff(
            journal=journal, ledger=runtime.ledger, authorize=lambda _proposal: None,
        )
        dispatch = PaperOrderDispatchService(
            ledger=runtime.ledger,
            facts_provider=lambda *_args, **_kwargs: pytest.fail("no order should be dispatched"),
            sender=lambda *_args, **_kwargs: pytest.fail("managed port owns the transport"),
            now=lambda: NOW,
        )
        feed = PaperCompletedBarFeed(
            instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        )
        return ManagedPythonPaperSession(
            runtime=runtime, run_id="deployment-run", version_store=versions,
            version_id=version.version_id, journal=journal, coordinator=coordinator,
            handoff=handoff, dispatch=dispatch, feeds={"AAPL": feed},
            data_by_con_id={123: feed},
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
            queue_size=4, idle_poll_seconds=0.01,
        )

    return build


def _manager(runtime_factory):
    principal = Principal("deployment-human", AuthMethod.FEDERATED_IDENTITY)
    manager = PaperExecutionManager(
        runtime_factory=runtime_factory,
        control_authorizer=lambda actor, _scope, action: (
            actor.attributable and actor.subject == principal.subject
            and action in {"activate", "cancel", "stop"}
        ),
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    return manager, principal


def test_manager_owns_session_lifecycle_and_exact_cancel_delegation(tmp_path):
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "managed-deployment.sqlite",
    )
    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime,
        session_builder=_session_builder(tmp_path),
    )
    scope = _scope()

    # Factory construction (including the trusted session builder) is offline.
    deployment = factory(scope)
    assert deployment.runtime is runtime
    assert runtime.process.pid is None
    assert runtime._started is False
    assert service.lease_held is False

    manager, principal = _manager(lambda _scope: deployment)
    status = manager.start(scope)
    assert status.phase == "READY"
    assert status.control_state is ExecutionControlState.PAUSED
    assert status.owner_lease_held is True
    assert runtime._started is True
    assert deployment.is_alive is True

    manager.activate(scope, principal)
    assert service.state()[0] is ExecutionControlState.RUNNING
    paused = manager.pause(scope, reason="deployment-test")
    assert paused.control_state is ExecutionControlState.PAUSED

    forwarded = []
    runtime.cancel_owned_order = lambda intent_id: forwarded.append(intent_id) or "runtime-result"
    assert manager.cancel_owned_order(scope, "intent-exact", principal) == "runtime-result"
    assert forwarded == ["intent-exact"]

    assert manager.stop(scope, principal) is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    assert service.lease_held is False
    assert manager.status() is None
    allocator.close()


def test_factory_pins_and_verifies_the_immutable_strategy_version(tmp_path):
    versions = StrategyVersionStore(tmp_path / "versions")
    frozen = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    runtime, _service, _ledger, _index, _allocator = _make_runtime(
        tmp_path, tmp_path / "pinned-version.sqlite",
    )

    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime,
        session_builder=_session_builder(tmp_path),
        strategy_version_id=frozen.version_id,
    )
    deployment = factory(_scope())
    assert deployment.version_id == frozen.version_id
    assert factory.strategy_version_id == frozen.version_id
    assert runtime.process.pid is None

    mismatch_runtime, *_ = _make_runtime(
        tmp_path, tmp_path / "pinned-version-mismatch.sqlite",
    )
    mismatch = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: mismatch_runtime,
        session_builder=_session_builder(tmp_path),
        strategy_version_id="a" * 64,
    )
    with pytest.raises(ManagedPythonPaperDeploymentError, match="pinned immutable strategy version"):
        mismatch(_scope())
    assert mismatch_runtime.process.pid is None


def test_wrong_runtime_scope_is_rejected_before_session_builder_or_process_start(tmp_path):
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "wrong-scope.sqlite",
    )
    calls = []
    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime,
        session_builder=lambda *_args: calls.append("builder"),
    )

    with pytest.raises(ManagedPythonPaperDeploymentError, match="runtime scope differs"):
        factory(_scope("another-account"))

    assert calls == []
    assert runtime.process.pid is None
    assert runtime._started is False
    assert service.lease_held is False
    allocator.close()


def test_session_with_split_journal_database_is_rejected_without_starting_runtime(tmp_path):
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "runtime.sqlite",
    )
    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime,
        session_builder=_session_builder(tmp_path, separate_journal=True),
    )

    with pytest.raises(RuntimeError, match="share SQLite"):
        factory(_scope())

    assert runtime.process.pid is None
    assert runtime._started is False
    assert service.lease_held is False
    allocator.close()


def test_session_builder_failure_does_not_start_or_lease_runtime(tmp_path):
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-failure.sqlite",
    )

    def fail_builder(_runtime, _scope):
        raise ValueError("session construction rejected")

    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime, session_builder=fail_builder,
    )
    manager, _principal = _manager(factory)

    with pytest.raises(PaperExecutionManagerError, match="safe paused startup") as error:
        manager.start(_scope())
    assert isinstance(error.value.__cause__, ValueError)
    assert "session construction rejected" in str(error.value.__cause__)

    assert manager.status() is None
    assert runtime.process.pid is None
    assert runtime._started is False
    assert service.lease_held is False
    allocator.close()


def test_recovery_failure_retains_manager_slot_and_owner_lease(tmp_path):
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "recovery-failure.sqlite",
    )
    runtime._external_recovery_check = lambda: False
    factory = ManagedPythonPaperDeploymentFactory(
        runtime_factory=lambda _scope: runtime,
        session_builder=_session_builder(tmp_path),
    )
    manager, principal = _manager(factory)

    with pytest.raises(PaperExecutionManagerError, match="safe paused startup"):
        manager.start(_scope())

    status = manager.status()
    assert status is not None
    assert status.phase == "START_FAILED"
    # Runtime reconciliation failures must converge to PAUSED while retaining
    # the manager slot and owner lease; RECOVERING is only the in-progress gate.
    assert status.control_state is ExecutionControlState.PAUSED
    assert status.reason == "managed_runtime_recovery_failed"
    assert status.owner_lease_held is True
    assert runtime._started is True
    assert service.lease_held is True
    with pytest.raises(PaperExecutionManagerError, match="recovery remains incomplete"):
        manager.retry_recovery(_scope())
    assert service.lease_held is True

    # The assertions above prove the manager retains ownership after failed
    # recovery. This fake runtime is safe to shut down afterward so the test
    # does not leave its non-daemon callback worker running at interpreter exit.
    assert manager.stop(_scope(), principal) is True
    assert service.lease_held is False
    assert manager.status() is None
    allocator.close()
