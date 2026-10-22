"""Lazy-init service singletons shared by session, channel, and live routes."""

from __future__ import annotations

import os

from fastapi import HTTPException

from src.api._compat import host_attr as _host_attr, set_host_attr as _set_host_attr
from src.api.helpers import RUNS_DIR, SESSIONS_DIR
from src.config.accessor import get_env_config


# ============================================================================
# Global singletons
# ============================================================================

_session_service = None
_channel_runtime = None
_channel_bus = None
_channel_manager = None
_paper_execution_manager = None
_paper_execution_readiness = None


# ============================================================================
# Lazy initializers
# ============================================================================


def _get_session_service():
    """Lazy-init session service when ENABLE_SESSION_RUNTIME=true."""
    global _session_service

    import sys as _sys
    _host = _sys.modules.get("api_server")
    if _host is not None and hasattr(_host, "_session_service"):
        host_val = getattr(_host, "_session_service")
        if host_val is not None:
            return host_val
    elif _session_service is not None:
        return _session_service

    if not get_env_config().api.enable_session_runtime:
        return None

    import asyncio

    from src.session.events import EventBus
    from src.session.service import SessionService
    from src.session.store import SessionStore

    # Honor monkeypatched SESSIONS_DIR / RUNS_DIR on api_server.
    sessions_dir = _host_attr("SESSIONS_DIR", SESSIONS_DIR)
    runs_dir = _host_attr("RUNS_DIR", RUNS_DIR)

    store = SessionStore(base_dir=sessions_dir)
    event_bus = EventBus()

    try:
        loop = asyncio.get_event_loop()
        event_bus.set_loop(loop)
    except RuntimeError:
        pass

    _session_service = SessionService(
        store=store,
        event_bus=event_bus,
        runs_dir=runs_dir,
    )
    _set_host_attr("_session_service", _session_service)
    _attach_delivery_listener(event_bus)
    return _session_service


def _get_paper_execution_manager(app=None):
    """Return an inert, fail-closed manager until a trusted factory is wired.

    Construction has no broker side effects. The default manager has no runtime
    factory and denies all control grants; this helper is only for status and
    future explicitly configured lifecycle integration.
    """
    global _paper_execution_manager

    if app is not None:
        app_manager = getattr(app.state, "paper_execution_manager", None)
        if app_manager is not None:
            return app_manager
        from src.execution.paper_execution_manager import PaperExecutionManager

        app_manager = PaperExecutionManager(runtime_factory=None)
        app.state.paper_execution_manager = app_manager
        return app_manager

    import sys as _sys
    _host = _sys.modules.get("api_server")
    if _host is not None and hasattr(_host, "_paper_execution_manager"):
        host_val = getattr(_host, "_paper_execution_manager")
        if host_val is not None:
            return host_val
    elif _paper_execution_manager is not None:
        return _paper_execution_manager

    from src.execution.paper_execution_manager import PaperExecutionManager

    _paper_execution_manager = PaperExecutionManager(runtime_factory=None)
    _set_host_attr("_paper_execution_manager", _paper_execution_manager)
    return _paper_execution_manager


def _get_paper_execution_readiness(app=None):
    """Return the explicitly installed app-composition readiness declaration.

    The default is intentionally incomplete. Registering a bare runtime
    factory does not imply that strategy, authorization, data, or risk
    collaborators are available.
    """
    global _paper_execution_readiness

    if app is not None:
        app_readiness = getattr(app.state, "paper_execution_readiness", None)
        if app_readiness is not None:
            return app_readiness
        # If a trusted manager was installed directly on this app, its bound
        # readiness is the startup gate and therefore the safest default for
        # status as well. The normal composition setter stores both values;
        # this fallback keeps an omitted app.state alias from appearing as a
        # different (all-missing) readiness declaration.
        app_manager = getattr(app.state, "paper_execution_manager", None)
        manager_readiness = getattr(app_manager, "startup_readiness", None)
        from src.execution.paper_execution_readiness import PaperExecutionReadiness

        if isinstance(manager_readiness, PaperExecutionReadiness):
            app.state.paper_execution_readiness = manager_readiness
            return manager_readiness
        app_readiness = PaperExecutionReadiness()
        app.state.paper_execution_readiness = app_readiness
        return app_readiness

    import sys as _sys
    _host = _sys.modules.get("api_server")
    if _host is not None and hasattr(_host, "_paper_execution_readiness"):
        host_val = getattr(_host, "_paper_execution_readiness")
        if host_val is not None:
            return host_val
    elif _paper_execution_readiness is not None:
        return _paper_execution_readiness

    from src.execution.paper_execution_readiness import PaperExecutionReadiness

    _paper_execution_readiness = PaperExecutionReadiness()
    _set_host_attr("_paper_execution_readiness", _paper_execution_readiness)
    return _paper_execution_readiness


def _set_paper_execution_composition(manager, readiness, *, app=None, scope=None) -> None:
    """Install an explicit manager/readiness pair from a trusted app root.

    This setter does not start the runtime. The caller must build and validate
    the concrete collaborators before registering them.
    """
    from src.execution.paper_execution_manager import (
        PaperExecutionManager,
        PaperRuntimeConflict,
        PaperRuntimeScope,
    )
    from src.execution.paper_execution_readiness import PaperExecutionReadiness

    if not isinstance(manager, PaperExecutionManager):
        raise TypeError("manager must be a PaperExecutionManager")
    if not isinstance(readiness, PaperExecutionReadiness):
        raise TypeError("readiness must be a PaperExecutionReadiness")
    if scope is not None and not isinstance(scope, PaperRuntimeScope):
        raise TypeError("scope must be a trusted PaperRuntimeScope or None")

    if app is not None:
        current_manager = getattr(app.state, "paper_execution_manager", None)
        current_readiness = getattr(app.state, "paper_execution_readiness", None)
        current_scope = getattr(app.state, "paper_execution_scope", None)

        # Re-registering the exact pair is harmless, including while it owns
        # a runtime. Replacing an active manager would orphan its runtime from
        # the app lifespan, which only shuts down the currently registered
        # manager.
        if current_manager is manager and current_readiness is readiness:
            if scope is not None:
                active_status = manager.status()
                if (
                    active_status is not None
                    and (
                        active_status.scope != scope
                        or (current_scope is not None and current_scope != scope)
                    )
                ):
                    raise PaperRuntimeConflict(
                        "cannot replace PAPER scope while the app manager has an active runtime"
                    )
                app.state.paper_execution_scope = scope
            return
        if current_manager is not None and current_manager.status() is not None:
            raise PaperRuntimeConflict(
                "cannot replace PAPER composition while the app manager has an active runtime"
            )

    # The same immutable readiness instance drives both status reporting and
    # the manager's pre-factory startup gate. Do not let these sources drift.
    manager.bind_startup_readiness(readiness)

    if app is not None:
        app.state.paper_execution_manager = manager
        app.state.paper_execution_readiness = readiness
        if scope is None:
            if hasattr(app.state, "paper_execution_scope"):
                delattr(app.state, "paper_execution_scope")
        else:
            app.state.paper_execution_scope = scope
        return

    global _paper_execution_manager, _paper_execution_readiness
    _paper_execution_manager = manager
    _paper_execution_readiness = readiness
    _set_host_attr("_paper_execution_manager", manager)
    _set_host_attr("_paper_execution_readiness", readiness)


#: Terminal attempt events a finished scheduled briefing can follow.
_DELIVERY_TRIGGER_EVENTS = frozenset(
    {"attempt.completed", "attempt.failed", "attempt.cancelled"}
)


def _attach_delivery_listener(event_bus) -> None:
    """Let a finished run push its briefing without waiting for the next tick.

    The listener only *asks* for a sweep. Delivery correctness lives in the
    periodic sweep and the persisted outbox row; this is latency, and it is
    written so that failing to fire costs nothing but the wait.

    Args:
        event_bus: The session event bus to listen on.
    """

    def _on_event(event) -> None:
        if event.event_type not in _DELIVERY_TRIGGER_EVENTS:
            return
        from src.api.scheduled_routes import _get_scheduled_research_executor

        _get_scheduled_research_executor().request_sweep()

    event_bus.add_listener(_on_event)


def _get_channel_runtime():
    """Lazy-init IM channel runtime without starting platform adapters."""
    global _channel_runtime, _channel_bus, _channel_manager

    import sys as _sys
    _host = _sys.modules.get("api_server")
    if _host is not None and hasattr(_host, "_channel_runtime"):
        host_rt = getattr(_host, "_channel_runtime")
        if host_rt is not None:
            return host_rt
    elif _channel_runtime is not None:
        return _channel_runtime

    from src.channels.bus.queue import MessageBus
    from src.channels.config import load_channels_config
    from src.channels.manager import ChannelManager
    from src.channels.runtime import ChannelRuntime

    svc = _get_session_service()
    if not svc:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")

    _channel_bus = MessageBus()
    config = load_channels_config()
    _channel_manager = ChannelManager(config, _channel_bus, session_service=svc)
    global_operators, channel_operators = ChannelRuntime.operators_from_config(config)
    _channel_runtime = ChannelRuntime(
        bus=_channel_bus,
        session_service=svc,
        manager=_channel_manager,
        reply_timeout_s=config["reply_timeout_s"],
        operators=global_operators,
        channel_operators=channel_operators,
    )
    _set_host_attr("_channel_runtime", _channel_runtime)
    _set_host_attr("_channel_bus", _channel_bus)
    _set_host_attr("_channel_manager", _channel_manager)
    return _channel_runtime


def reset_channel_runtime() -> None:
    """Drop the cached IM channel runtime so the next access rebuilds from disk.

    ``_get_channel_runtime`` caches its singletons in this module's globals
    *and* on the ``api_server`` host attrs; clearing both — with ``None``,
    which is what makes the next call rebuild — is what lets an edited channel
    config apply without a process restart.
    """
    global _channel_runtime, _channel_bus, _channel_manager

    _channel_runtime = None
    _channel_bus = None
    _channel_manager = None
    _set_host_attr("_channel_runtime", None)
    _set_host_attr("_channel_bus", None)
    _set_host_attr("_channel_manager", None)
