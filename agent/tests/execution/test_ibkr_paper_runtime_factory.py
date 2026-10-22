from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.execution.contracts import Environment
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperEndpointProfile,
    IbkrPaperRuntimeConfig,
    IbkrPaperRuntimeConfigurationError,
    IbkrPaperRuntimeDependencies,
    IbkrPaperRuntimeFactory,
)
from src.execution.paper_execution_manager import PaperRuntimeScope


def _dependencies():
    return IbkrPaperRuntimeDependencies(
        callback_sink=lambda _callback: None,
        recovery_check=lambda: False,
        activation_check=lambda: False,
        quiesce_check=lambda: False,
        intent_loader=lambda _intent_id: None,
        contract_loader=lambda _instrument_id: None,
    )


def _config(
    tmp_path: Path,
    *,
    port: int = 4002,
    profile: IbkrPaperEndpointProfile = IbkrPaperEndpointProfile.IB_GATEWAY_PAPER,
    account: str = "DU123456",
    host: str = "127.0.0.1",
):
    return IbkrPaperRuntimeConfig(
        python=sys.executable,
        runtime_cwd=Path(__file__).resolve().parents[3] / "ibkr-runtime",
        gateway_host=host,
        gateway_port=port,
        paper_endpoint_profile=profile,
        client_id=27,
        account_ref=account,
        database_path=tmp_path / "paper.sqlite",
        lock_directory=tmp_path / "owner-locks",
    )


def test_factory_composes_one_shared_database_without_starting_sdk_process(tmp_path):
    factory = IbkrPaperRuntimeFactory(
        config=_config(tmp_path),
        dependencies=_dependencies(),
        snapshot_id_factory=lambda: 19,
    )

    runtime = factory(PaperRuntimeScope("IBKR", "DU123456", Environment.PAPER))

    assert runtime.process.pid is None
    assert runtime._started is False
    assert runtime.service.lease_held is False
    # The durable gate's empty-database default is PAUSED; no owner lease is
    # held, and this must not be confused with completed broker recovery.
    assert runtime.service.state()[0].value == "PAUSED"
    assert runtime.process.spool_path == str((tmp_path / "paper.sqlite").resolve())
    assert all(Path(path).resolve() == (tmp_path / "paper.sqlite").resolve()
               for path in runtime.durable_database_paths)
    assert callable(runtime.paper_transport)
    assert callable(runtime.cancel_transport)


def test_factory_requires_exact_configured_paper_scope_without_opening_process(tmp_path):
    factory = IbkrPaperRuntimeFactory(config=_config(tmp_path), dependencies=_dependencies())
    with pytest.raises(ValueError, match="differs from the explicit PAPER runtime configuration"):
        factory(PaperRuntimeScope("IBKR", "DU654321", Environment.PAPER))
    assert not (tmp_path / "paper.sqlite").exists()


def test_config_rejects_known_live_gateway_port(tmp_path):
    for port in (4001, 7496):
        with pytest.raises(IbkrPaperRuntimeConfigurationError, match="known LIVE endpoint"):
            _config(tmp_path, port=port)
    assert not (tmp_path / "paper.sqlite").exists()


@pytest.mark.parametrize("port", [4003, 7498, 5001])
def test_config_rejects_unknown_or_mismatched_paper_ports_before_database_creation(tmp_path, port):
    with pytest.raises(IbkrPaperRuntimeConfigurationError, match="requires its default PAPER port"):
        _config(tmp_path, port=port)
    assert not (tmp_path / "paper.sqlite").exists()


def test_config_requires_matching_explicit_tws_paper_profile(tmp_path):
    with pytest.raises(IbkrPaperRuntimeConfigurationError, match="requires its default PAPER port 7497"):
        _config(tmp_path, port=4002, profile=IbkrPaperEndpointProfile.TWS_PAPER)
    config = _config(tmp_path, port=7497, profile=IbkrPaperEndpointProfile.TWS_PAPER)
    assert config.gateway_port == 7497


@pytest.mark.parametrize("account", ["U123456", "paper-fixture", "DUABC", "DU123456,DU654321"])
def test_config_rejects_non_single_ibkr_paper_account_identity(tmp_path, account):
    with pytest.raises(IbkrPaperRuntimeConfigurationError, match="DU numeric account form"):
        _config(tmp_path, account=account)
    assert not (tmp_path / "paper.sqlite").exists()


def test_config_rejects_non_loopback_gateway_host(tmp_path):
    with pytest.raises(IbkrPaperRuntimeConfigurationError, match="loopback address"):
        _config(tmp_path, host="192.0.2.1")
    assert not (tmp_path / "paper.sqlite").exists()


def test_dependencies_do_not_supply_implicit_success_checks():
    with pytest.raises(TypeError):
        IbkrPaperRuntimeDependencies(  # type: ignore[call-arg]
            callback_sink=lambda _callback: None,
            recovery_check=lambda: True,
            activation_check=lambda: True,
            quiesce_check=lambda: True,
            intent_loader=lambda _intent_id: None,
        )
