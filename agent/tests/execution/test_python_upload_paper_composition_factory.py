from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import backtrader
import pytest

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.bt_runtime.strategies.sma import TRUSTED_TEMPLATE_COMPILER_VERSION, TRUSTED_TEMPLATE_SOURCE
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Environment, SourceKind, StrategySpec
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION, PythonEventCoordinator
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperEndpointProfile,
    IbkrPaperRuntimeConfig,
    IbkrPaperRuntimeDependencies,
)
from src.execution.nl2_paper_composition_factory import build_frozen_nl2_paper_composition
from src.execution.python_sandbox import PythonSandbox
from src.execution.python_upload_paper_composition_factory import (
    PythonUploadPaperCompositionError,
    build_frozen_python_upload_paper_composition,
)
from src.execution.python_upload_paper_session_builder import (
    PythonUploadPaperSessionBuildError,
    PythonUploadPaperSessionBuilder,
)
from src.execution.managed_python_paper_deployment import ManagedPythonPaperDeploymentError
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.managed_python_paper_deployment import ManagedPythonPaperDeploymentFactory
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.versions import StrategyVersionStore
from tests.execution.test_trusted_template_paper_session_builder import (
    ENGINE_VERSION,
    NOW,
    _nl2_spec,
    _providers,
)


ACCOUNT = "DU1234567"
UPLOAD_SOURCE = b"def on_event(event, state):\n    return {}\n"


def _runtime_config(tmp_path: Path, account_ref: str = ACCOUNT) -> IbkrPaperRuntimeConfig:
    return IbkrPaperRuntimeConfig(
        python=sys.executable,
        runtime_cwd=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=4002,
        paper_endpoint_profile=IbkrPaperEndpointProfile.IB_GATEWAY_PAPER,
        client_id=31,
        account_ref=account_ref,
        database_path=tmp_path / "paper.sqlite",
        lock_directory=tmp_path / "paper-owner-lock",
    )


def _runtime_dependencies() -> IbkrPaperRuntimeDependencies:
    return IbkrPaperRuntimeDependencies(
        callback_sink=lambda _event: pytest.fail("unbound callback sink must not be called"),
        recovery_check=lambda: True,
        activation_check=lambda: True,
        quiesce_check=lambda: True,
        intent_loader=lambda _intent_id: None,
        contract_loader=lambda _instrument_id: None,
    )


def _make_upload_store(tmp_path: Path, *, spec: StrategySpec | None = None):
    store = StrategyVersionStore(tmp_path / "versions")
    if spec is None:
        spec_data = _nl2_spec(source_kind="python_upload").model_dump(mode="json")
        spec_data["source_artifact_id"] = f"upload-{hashlib.sha256(UPLOAD_SOURCE).hexdigest()[:16]}"
        spec = StrategySpec.model_validate(spec_data)
    upload_spec = spec
    record = store.create_version(
        upload_spec,
        upload_source=UPLOAD_SOURCE,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )
    return store, record


def _composition(tmp_path: Path, store: StrategyVersionStore, record, *, version_id=None):
    feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York", queue_size=4)
    proposal_inputs, dispatch_facts, _calls = _providers(record.version_hash, account_ref=ACCOUNT)
    sandbox = PythonSandbox("sha256:" + "a" * 64)
    return build_frozen_python_upload_paper_composition(
        scope=PaperRuntimeScope("IBKR", ACCOUNT, Environment.PAPER),
        runtime_config=_runtime_config(tmp_path),
        runtime_dependencies=_runtime_dependencies(),
        version_store=store,
        version_id=version_id or record.version_id,
        run_id="upload-paper-composition-run",
        deployment_id="builder-deployment",
        sandbox=sandbox,
        proposal_risk_inputs=proposal_inputs,
        dispatch_risk_facts=dispatch_facts,
        feeds={"AAPL": feed},
        data_by_con_id={123: feed},
        resolve_contract_identity=lambda symbol: ContractIdentity(123, symbol, "STK", "USD"),
        control_authorizer=lambda _principal, _scope, _action: False,
        now=lambda: NOW,
    )


def test_upload_factory_builds_exact_frozen_offline_session_without_start_or_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, record = _make_upload_store(tmp_path)
    composition = _composition(tmp_path, store, record)
    factory = composition.manager.runtime_factory
    assert isinstance(factory, ManagedPythonPaperDeploymentFactory)
    assert factory.strategy_version_id == record.version_id
    assert isinstance(factory.session_builder, PythonUploadPaperSessionBuilder)

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("SDK process must stay stopped"))
    session = factory(composition.scope)
    runtime = session.runtime
    try:
        assert isinstance(session, ManagedPythonPaperSession)
        assert session.version_id == record.version_id
        assert session.record.source_kind is SourceKind.PYTHON_UPLOAD
        assert isinstance(session.coordinator, PythonEventCoordinator)
        assert session.coordinator.sandbox.image == "sha256:" + "a" * 64
        assert runtime.process.pid is None
        assert runtime._started is False
        assert runtime.service.lease_held is False
        assert session.journal.path == runtime.ledger.path
        assert runtime.callback_pump.callback_sink is session.callback_router
    finally:
        runtime.allocator.close()


def test_upload_factory_rejects_nl2_and_unknown_or_mismatched_versions(tmp_path: Path):
    store, upload_record = _make_upload_store(tmp_path)
    with pytest.raises((PythonUploadPaperCompositionError, ValueError), match="version"):
        _composition(tmp_path, store, upload_record, version_id="a" * 64)

    nl2_record = store.create_version(
        _nl2_spec(),
        template_source=TRUSTED_TEMPLATE_SOURCE,
        engine_version=ENGINE_VERSION,
        compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    with pytest.raises(PythonUploadPaperCompositionError, match="frozen Python-upload"):
        _composition(tmp_path, store, upload_record, version_id=nl2_record.version_id)


@pytest.mark.parametrize(
    ("engine_version", "compiler_version"),
    [
        (f"backtrader/{backtrader.__version__}", "other-worker/1"),
        ("backtrader/other", PYTHON_WORKER_COMPILER_VERSION),
    ],
)
def test_upload_factory_rejects_wrong_frozen_engine_or_worker_compiler(
    tmp_path: Path, engine_version: str, compiler_version: str,
):
    spec_data = _nl2_spec(source_kind="python_upload").model_dump(mode="json")
    spec_data["source_artifact_id"] = f"upload-{hashlib.sha256(UPLOAD_SOURCE).hexdigest()[:16]}"
    store = StrategyVersionStore(tmp_path / "versions")
    record = store.create_version(
        StrategySpec.model_validate(spec_data),
        upload_source=UPLOAD_SOURCE,
        engine_version=engine_version,
        compiler_version=compiler_version,
    )
    with pytest.raises(PythonUploadPaperCompositionError, match="frozen Python-upload"):
        _composition(tmp_path, store, record)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["position_policy"].update(rebalance="on_signal_transition"),
        lambda data: data["execution_policy"].update(gap_action="skip"),
        lambda data: data["bar_policy"].update(calendar_id="other"),
    ],
)
def test_upload_factory_rejects_frozen_sizing_or_execution_policy_mismatch(tmp_path: Path, mutation):
    data = _nl2_spec(source_kind="python_upload").model_dump(mode="json")
    data["source_artifact_id"] = f"upload-{hashlib.sha256(UPLOAD_SOURCE).hexdigest()[:16]}"
    mutation(data)
    spec = StrategySpec.model_validate(data)
    store, record = _make_upload_store(tmp_path, spec=spec)
    with pytest.raises(
        (PythonUploadPaperCompositionError, PythonUploadPaperSessionBuildError),
        match="frozen Python-upload",
    ):
        _composition(tmp_path, store, record)


def test_builder_rejects_runtime_collaborators_with_different_database(tmp_path: Path):
    store, record = _make_upload_store(tmp_path)
    composition = _composition(tmp_path, store, record)
    factory = composition.manager.runtime_factory
    runtime = factory.runtime_factory(composition.scope)
    try:
        runtime.order_index.path = str(tmp_path / "other.sqlite")
        builder = factory.session_builder
        with pytest.raises(PythonUploadPaperSessionBuildError, match="share one SQLite"):
            builder(runtime, composition.scope)
        assert runtime.process.pid is None
        assert runtime.service.lease_held is False
    finally:
        runtime.allocator.close()


def test_factory_pins_upload_version_and_deployment_rejects_substitution(tmp_path: Path):
    store, record = _make_upload_store(tmp_path)
    composition = _composition(tmp_path, store, record)
    factory = composition.manager.runtime_factory
    assert factory.strategy_version_id == record.version_id
    with pytest.raises(ManagedPythonPaperDeploymentError, match="does not match the factory-pinned"):
        ManagedPythonPaperDeploymentFactory(
            runtime_factory=factory.runtime_factory,
            session_builder=factory.session_builder,
            strategy_version_id="b" * 64,
        )(composition.scope)
