"""Offline construction of a manager-owned Python PAPER deployment.

The factory joins an explicitly configured IBKR runtime factory to a trusted
builder for the frozen Python/Cerebro session. Calling it creates local
objects only. The returned session is the object registered with
``PaperExecutionManager``; ``start_paused`` remains the explicit runtime
connection and recovery boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from src.execution.contracts import is_sha256

from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.paper_execution_manager import PaperRuntimeScope


class ManagedPythonPaperDeploymentError(RuntimeError):
    """The runtime and strategy session do not form one safe deployment."""


RuntimeFactory = Callable[[PaperRuntimeScope], IbkrManagedPaperRuntime]
SessionBuilder = Callable[
    [IbkrManagedPaperRuntime, PaperRuntimeScope], ManagedPythonPaperSession
]


class ManagedPythonPaperDeploymentFactory:
    """Build a manager-compatible session around one unstarted IBKR runtime.

    Both inputs are trusted application collaborators. The runtime factory
    must only construct an offline ``IbkrManagedPaperRuntime``; the session
    builder must bind callback routing and all same-database strategy
    collaborators before returning. This class verifies those invariants and
    never starts the SDK process or obtains the account lease.
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory,
        session_builder: SessionBuilder,
        strategy_version_id: str | None = None,
    ) -> None:
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable")
        if not callable(session_builder):
            raise TypeError("session_builder must be callable")
        if strategy_version_id is not None and not is_sha256(strategy_version_id):
            raise ValueError("strategy_version_id must be a lowercase SHA-256 version ID")
        self.runtime_factory = runtime_factory
        self.session_builder = session_builder
        # Pin this factory to one immutable version. A caller may echo this ID
        # to the API, but cannot use a request to select a different version.
        self.strategy_version_id = strategy_version_id

    def __call__(self, scope: PaperRuntimeScope) -> ManagedPythonPaperSession:
        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a PaperRuntimeScope")

        runtime = self.runtime_factory(scope)
        self._validate_runtime(runtime, scope)

        # Session construction installs its callback sink/deferred ACK before
        # runtime startup. Calling start_paused before this point would lose
        # the safe callback staging window.
        session = self.session_builder(runtime, scope)
        self._validate_session(session, runtime, scope)
        if self.strategy_version_id is not None:
            record = session.version_store.get_version(session.version_id)
            if (
                session.version_id != self.strategy_version_id
                or record.version_id != self.strategy_version_id
            ):
                raise ManagedPythonPaperDeploymentError(
                    "session does not match the factory-pinned immutable strategy version"
                )
        return session

    @staticmethod
    def _validate_runtime(runtime: IbkrManagedPaperRuntime, scope: PaperRuntimeScope) -> None:
        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("runtime_factory must return an IbkrManagedPaperRuntime")
        if (
            runtime.service.broker != scope.broker
            or runtime.service.account_ref != scope.account_ref
            or runtime.service.environment is not scope.environment
            or runtime.account_ref != scope.account_ref
        ):
            raise ManagedPythonPaperDeploymentError("runtime scope differs from requested PAPER scope")
        if runtime._started or runtime.process.pid is not None or runtime.service.lease_held:
            raise ManagedPythonPaperDeploymentError("runtime factory must return an unstarted, unleased runtime")
        if not runtime.durable_database_paths:
            raise ManagedPythonPaperDeploymentError("runtime has no durable database path proof")

    @staticmethod
    def _validate_session(
        session: ManagedPythonPaperSession,
        runtime: IbkrManagedPaperRuntime,
        scope: PaperRuntimeScope,
    ) -> None:
        if not isinstance(session, ManagedPythonPaperSession):
            raise TypeError("session_builder must return a ManagedPythonPaperSession")
        if session.runtime is not runtime:
            raise ManagedPythonPaperDeploymentError("session builder returned a session for another runtime")
        if (
            session.service is not runtime.service
            or session.service.broker != scope.broker
            or session.service.account_ref != scope.account_ref
            or session.service.environment is not scope.environment
        ):
            raise ManagedPythonPaperDeploymentError("session scope differs from requested PAPER scope")
        runtime_paths = tuple(runtime.durable_database_paths)
        session_paths = tuple(session.durable_database_paths)
        if not session_paths or session_paths != runtime_paths:
            raise ManagedPythonPaperDeploymentError("session and runtime must share the same durable database paths")
        if session._started or runtime._started or runtime.process.pid is not None or runtime.service.lease_held:
            raise ManagedPythonPaperDeploymentError("deployment construction must not start or lease the runtime")


__all__ = ["ManagedPythonPaperDeploymentError", "ManagedPythonPaperDeploymentFactory"]
