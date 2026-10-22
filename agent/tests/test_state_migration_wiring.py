"""Entry points run the one-time legacy-state migration (issue #904)."""

from __future__ import annotations

import pytest

from src.config import migrate


@pytest.fixture()
def record_migration(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(
        migrate, "migrate_legacy_state", lambda *a, **k: calls.append(1) or []
    )
    return calls


def test_cli_main_runs_migration(
    monkeypatch: pytest.MonkeyPatch, record_migration: list[int]
) -> None:
    import importlib

    import cli._legacy as legacy

    cli_main = importlib.import_module("cli.main")

    monkeypatch.setattr(cli_main, "_is_interactive_invocation", lambda argv: False)
    monkeypatch.setattr(legacy, "main", lambda argv: 0)

    assert cli_main.main(["list"]) == 0
    assert record_migration


def test_mcp_server_main_runs_migration(
    monkeypatch: pytest.MonkeyPatch, record_migration: list[int]
) -> None:
    import mcp_server

    monkeypatch.setattr(
        "sys.argv", ["vibe-trading-mcp", "--transport", "stdio"]
    )
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: None)

    mcp_server.main()
    assert record_migration


def test_api_startup_runs_migration(
    monkeypatch: pytest.MonkeyPatch, record_migration: list[int]
) -> None:
    import asyncio

    import api_server

    monkeypatch.setattr("src.preflight.run_preflight", lambda console: None)
    monkeypatch.setattr(api_server, "_start_scheduled_research_executor", lambda: None)
    monkeypatch.setattr(
        "src.config.accessor.get_env_config",
        lambda: type(
            "Cfg",
            (),
            {
                "agent_tuning": type(
                    "Tuning", (), {"vibe_trading_channels_auto_start": False}
                )()
            },
        )(),
    )

    asyncio.run(api_server._run_startup_preflight())
    assert record_migration


def test_api_lifespan_preserves_startup_and_shutdown_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import api_server

    events: list[str] = []

    monkeypatch.setattr(
        migrate,
        "migrate_legacy_state",
        lambda: events.append("migration"),
    )
    monkeypatch.setattr(
        "src.preflight.run_preflight",
        lambda console: events.append("preflight"),
    )
    monkeypatch.setattr(
        api_server,
        "_start_scheduled_research_executor",
        lambda: events.append("scheduler-start"),
    )

    async def start_channels() -> None:
        events.append("channels-start")

    async def stop_channels() -> None:
        events.append("channels-stop")

    async def stop_scheduler() -> None:
        events.append("scheduler-stop")

    class PaperManager:
        def shutdown_active_runtime(self) -> bool:
            events.append("paper-runtime-stop")
            return True

    monkeypatch.setattr(api_server, "_start_channel_runtime", start_channels)
    monkeypatch.setattr(api_server, "_stop_channel_runtime", stop_channels)
    monkeypatch.setattr(api_server, "_stop_scheduled_research_executor", stop_scheduler)
    monkeypatch.setattr(api_server.app.state, "paper_execution_manager", PaperManager(), raising=False)
    monkeypatch.setattr(
        "src.config.accessor.get_env_config",
        lambda: type(
            "Cfg",
            (),
            {
                "agent_tuning": type(
                    "Tuning", (), {"vibe_trading_channels_auto_start": True}
                )()
            },
        )(),
    )

    async def scenario() -> None:
        async with api_server._lifespan(api_server.app):
            events.append("serving")

    asyncio.run(scenario())

    assert events == [
        "migration",
        "preflight",
        "scheduler-start",
        "channels-start",
        "serving",
        "paper-runtime-stop",
        "channels-stop",
        "scheduler-stop",
    ]


def test_api_lifespan_stops_scheduler_when_channel_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import api_server

    events: list[str] = []

    async def startup() -> None:
        events.append("startup")

    async def stop_channels() -> None:
        events.append("channels-stop")
        raise RuntimeError("channel shutdown failed")

    async def stop_scheduler() -> None:
        events.append("scheduler-stop")

    class PaperManager:
        def shutdown_active_runtime(self) -> bool:
            events.append("paper-runtime-stop")
            return True

    monkeypatch.setattr(api_server, "_run_startup_preflight", startup)
    monkeypatch.setattr(api_server, "_stop_channel_runtime", stop_channels)
    monkeypatch.setattr(api_server, "_stop_scheduled_research_executor", stop_scheduler)
    monkeypatch.setattr(api_server.app.state, "paper_execution_manager", PaperManager(), raising=False)

    async def scenario() -> None:
        async with api_server._lifespan(api_server.app):
            events.append("serving")

    with pytest.raises(RuntimeError, match="channel shutdown failed"):
        asyncio.run(scenario())

    assert events == ["startup", "serving", "paper-runtime-stop", "channels-stop", "scheduler-stop"]


def test_api_lifespan_shuts_down_manager_owned_by_each_app(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import api_server
    from fastapi import FastAPI

    stopped: list[str] = []

    class PaperManager:
        def __init__(self, name: str) -> None:
            self.name = name

        def shutdown_active_runtime(self) -> bool:
            stopped.append(self.name)
            return True

    async def no_op() -> None:
        return None

    monkeypatch.setattr(api_server, "_run_startup_preflight", no_op)
    monkeypatch.setattr(api_server, "_stop_scheduled_research_on_shutdown", no_op)
    first = FastAPI()
    second = FastAPI()
    first.state.paper_execution_manager = PaperManager("first")
    second.state.paper_execution_manager = PaperManager("second")

    async def scenario() -> None:
        async with api_server._lifespan(first):
            pass
        assert stopped == ["first"]
        async with api_server._lifespan(second):
            pass

    asyncio.run(scenario())
    assert stopped == ["first", "second"]


def test_api_lifespan_installs_explicit_paper_composition_without_starting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import api_server
    from fastapi import FastAPI
    from src.execution.paper_execution_composition import PaperExecutionComposition
    from src.execution.paper_execution_manager import PaperExecutionManager, PaperRuntimeScope
    from src.execution.paper_execution_readiness import PaperExecutionReadiness

    factory_calls: list[str] = []
    async def no_op() -> None:
        return None

    monkeypatch.setattr(api_server, "_run_startup_preflight", no_op)
    monkeypatch.setattr(api_server, "_stop_scheduled_research_on_shutdown", no_op)
    runtime_factory = lambda *_args, **_kwargs: factory_calls.append("factory")
    runtime_factory.strategy_version_id = "f" * 64
    manager = PaperExecutionManager(runtime_factory=runtime_factory)
    readiness = PaperExecutionReadiness()
    scope = PaperRuntimeScope("IBKR", "DU1234567")
    composition = PaperExecutionComposition(manager, readiness, scope)
    target_app = FastAPI()
    api_server.configure_paper_execution_app(composition, target_app=target_app)

    async def scenario() -> None:
        async with api_server._lifespan(target_app):
            assert target_app.state.paper_execution_manager is manager
            assert target_app.state.paper_execution_readiness is readiness
            assert target_app.state.paper_execution_scope is scope
            assert manager.status() is None
            assert factory_calls == []

    asyncio.run(scenario())
    assert factory_calls == []


def test_api_lifespan_default_paper_composition_remains_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import api_server
    from fastapi import FastAPI

    async def no_op() -> None:
        return None

    monkeypatch.setattr(api_server, "_run_startup_preflight", no_op)
    monkeypatch.setattr(api_server, "_stop_scheduled_research_on_shutdown", no_op)
    target_app = FastAPI()

    async def scenario() -> None:
        async with api_server._lifespan(target_app):
            manager = target_app.state.paper_execution_manager
            readiness = target_app.state.paper_execution_readiness
            assert manager.runtime_factory is None
            assert readiness.composition_ready is False
            assert manager.status() is None

    asyncio.run(scenario())
