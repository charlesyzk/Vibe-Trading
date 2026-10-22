from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Environment
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperEndpointProfile,
    IbkrPaperRuntimeConfig,
    IbkrPaperRuntimeDependencies,
)
from src.execution.nl2_paper_composition_factory import (
    NL2PaperCompositionError,
    build_frozen_nl2_paper_composition,
)
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.trusted_template_paper_session_builder import TrustedTemplatePaperSessionBuilder
from src.execution.versions import StrategyVersionStore
from tests.execution.test_trusted_template_paper_session_builder import (
    NOW,
    _providers,
    _version_store,
)


ACCOUNT = "DU1234567"
_UNSET = object()


def _runtime_config(tmp_path: Path, account_ref: str = ACCOUNT) -> IbkrPaperRuntimeConfig:
    return IbkrPaperRuntimeConfig(
        python=sys.executable,
        runtime_cwd=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=4002,
        paper_endpoint_profile=IbkrPaperEndpointProfile.IB_GATEWAY_PAPER,
        client_id=17,
        account_ref=account_ref,
        database_path=tmp_path / "paper.sqlite",
        lock_directory=tmp_path / "paper-owner-lock",
    )


def _runtime_dependencies(*, callback_sink=None) -> IbkrPaperRuntimeDependencies:
    return IbkrPaperRuntimeDependencies(
        callback_sink=callback_sink or (lambda _event: pytest.fail("unbound callback sink was invoked")),
        recovery_check=lambda: True,
        activation_check=lambda: True,
        quiesce_check=lambda: True,
        intent_loader=lambda _intent_id: None,
        contract_loader=lambda _instrument_id: None,
    )


def _composition(
    tmp_path: Path,
    store: StrategyVersionStore,
    record,
    *,
    scope=_UNSET,
    runtime_config=_UNSET,
    runtime_dependencies=_UNSET,
    version_id: str | None = None,
):
    feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York", queue_size=4)
    proposal_inputs, dispatch_facts, _calls = _providers(record.version_hash, account_ref=ACCOUNT)
    return build_frozen_nl2_paper_composition(
        scope=PaperRuntimeScope("IBKR", ACCOUNT, Environment.PAPER) if scope is _UNSET else scope,
        runtime_config=_runtime_config(tmp_path) if runtime_config is _UNSET else runtime_config,
        runtime_dependencies=(
            _runtime_dependencies() if runtime_dependencies is _UNSET else runtime_dependencies
        ),
        version_store=store,
        version_id=version_id or record.version_id,
        run_id="nl2-paper-composition-run",
        deployment_id="nl2-paper-composition-deployment",
        proposal_risk_inputs=proposal_inputs,
        dispatch_risk_facts=dispatch_facts,
        feeds={"AAPL": feed},
        data_by_con_id={123: feed},
        resolve_contract_identity=lambda symbol: ContractIdentity(123, symbol, "STK", "USD"),
        control_authorizer=lambda _principal, _scope, _action: False,
        now=lambda: NOW,
    )


def test_builder_returns_ready_pinned_composition_without_starting_or_leasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, record = _version_store(tmp_path)
    composition = _composition(tmp_path, store, record)

    assert composition.scope.account_ref == ACCOUNT
    assert composition.manager.startup_readiness is composition.readiness
    assert composition.readiness.composition_ready is True
    deployment_factory = composition.manager.runtime_factory
    assert deployment_factory.strategy_version_id == record.version_id
    assert isinstance(deployment_factory.session_builder, TrustedTemplatePaperSessionBuilder)

    # Construct the complete runtime + frozen session graph as the manager
    # would, but make any subprocess launch an immediate test failure.
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("process must remain stopped"))
    session_builder = deployment_factory.session_builder

    def observe_fail_closed_sink(runtime, scope):
        with pytest.raises(RuntimeError, match="before its frozen strategy session"):
            runtime.callback_pump.callback_sink(None)
        return session_builder(runtime, scope)

    deployment_factory.session_builder = observe_fail_closed_sink
    session = deployment_factory(composition.scope)
    runtime = session.runtime
    try:
        assert session.version_id == record.version_id
        assert runtime.process.pid is None
        assert runtime._started is False
        assert runtime.service.lease_held is False
        assert runtime.callback_pump.callback_sink is session.callback_router
    finally:
        runtime.allocator.close()


def test_builder_rejects_scope_config_account_mismatch(tmp_path: Path):
    store, record = _version_store(tmp_path)
    with pytest.raises(NL2PaperCompositionError, match="scope account_ref"):
        _composition(
            tmp_path,
            store,
            record,
            scope=PaperRuntimeScope("IBKR", "DU7654321", Environment.PAPER),
        )


@pytest.mark.parametrize("version_id", ["a" * 64, "not-a-version"])
def test_builder_rejects_unknown_or_malformed_frozen_version(tmp_path: Path, version_id: str):
    store, record = _version_store(tmp_path)
    with pytest.raises((NL2PaperCompositionError, ValueError), match="version"):
        _composition(tmp_path, store, record, version_id=version_id)


def test_builder_rejects_non_nl2_frozen_version(tmp_path: Path):
    store, record = _version_store(tmp_path, source_kind="python_upload")
    with pytest.raises(NL2PaperCompositionError, match="frozen NL2"):
        _composition(tmp_path, store, record)


@pytest.mark.parametrize(
    ("field", "value", "error", "message"),
    [
        ("runtime_config", None, TypeError, "runtime_config"),
        ("runtime_dependencies", None, TypeError, "runtime_dependencies"),
    ],
)
def test_builder_rejects_missing_runtime_configuration_or_dependencies(
    tmp_path: Path, field: str, value, error: type[Exception], message: str,
):
    store, record = _version_store(tmp_path)
    with pytest.raises(error, match=message):
        _composition(tmp_path, store, record, **{field: value})


def test_builder_rejects_missing_strategy_and_control_dependencies(tmp_path: Path):
    store, record = _version_store(tmp_path)
    feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York", queue_size=4)
    proposal_inputs, dispatch_facts, _calls = _providers(record.version_hash, account_ref=ACCOUNT)
    required = {
        "scope": PaperRuntimeScope("IBKR", ACCOUNT, Environment.PAPER),
        "runtime_config": _runtime_config(tmp_path),
        "runtime_dependencies": _runtime_dependencies(),
        "version_store": store,
        "version_id": record.version_id,
        "run_id": "missing-dependency-run",
        "deployment_id": "missing-dependency-deployment",
        "proposal_risk_inputs": proposal_inputs,
        "dispatch_risk_facts": dispatch_facts,
        "feeds": {"AAPL": feed},
        "data_by_con_id": {123: feed},
        "resolve_contract_identity": lambda symbol: ContractIdentity(123, symbol, "STK", "USD"),
        "now": lambda: NOW,
    }
    with pytest.raises(TypeError, match="control_authorizer"):
        build_frozen_nl2_paper_composition(**required, control_authorizer=None)
    with pytest.raises(TypeError, match="proposal_risk_inputs"):
        build_frozen_nl2_paper_composition(
            **{**required, "proposal_risk_inputs": None},
            control_authorizer=lambda *_args: False,
        )


def test_replaced_runtime_dependencies_are_immutable(tmp_path: Path):
    store, record = _version_store(tmp_path)
    original_sink_calls = []
    original_dependencies = _runtime_dependencies(
        callback_sink=lambda event: original_sink_calls.append(event),
    )
    composition = _composition(
        tmp_path, store, record, runtime_dependencies=original_dependencies,
    )
    assert composition.manager.runtime_factory.runtime_factory.dependencies is not original_dependencies
    assert composition.manager.runtime_factory.runtime_factory.dependencies.callback_sink is not original_dependencies.callback_sink
    assert original_sink_calls == []
