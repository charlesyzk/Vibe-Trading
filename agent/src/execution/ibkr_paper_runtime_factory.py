"""Explicit, offline construction of the IBKR PAPER runtime graph.

Calling the factory creates only local Python objects and SQLite files. It does
not launch the SDK child or connect to a Gateway; ``start_paused()`` remains the
explicit connection boundary owned by :class:`IbkrManagedPaperRuntime`.
"""

from __future__ import annotations

import queue
import secrets
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Any, Callable

from src.execution.contracts import Environment
from src.execution.ibkr_callback_pump import (
    BrokerOrderIndex,
    IbkrCallbackPump,
    IbkrCallbackPumpWorker,
)
from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator
from src.execution.ibkr_reconciliation import (
    IbkrReconciliationCoordinator,
    IbkrRecoveryReconciler,
    SQLiteIbkrSnapshotStore,
)
from src.execution.ibkr_runtime_process import (
    IbkrRuntimeHealthMonitor,
    IbkrRuntimeProcess,
    IbkrSubprocessCancelTransport,
    IbkrSubprocessPaperTransport,
)
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.execution_service import PaperExecutionService
from src.execution.paper_execution_manager import PaperRuntimeScope


class IbkrPaperRuntimeConfigurationError(ValueError):
    """The explicit PAPER runtime configuration is incomplete or unsafe."""


class IbkrPaperEndpointProfile(str, Enum):
    """Supported local IBKR PAPER API endpoints and their default ports."""

    IB_GATEWAY_PAPER = "ib_gateway_paper"
    TWS_PAPER = "tws_paper"


_PAPER_PROFILE_PORTS = {
    IbkrPaperEndpointProfile.IB_GATEWAY_PAPER: 4002,
    IbkrPaperEndpointProfile.TWS_PAPER: 7497,
}
_PAPER_ACCOUNT_RE = re.compile(r"^DU[0-9]+$")
_LOCAL_GATEWAY_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class IbkrPaperRuntimeConfig:
    """All operator-selected endpoint, identity, and durable path settings.

    No Gateway, account, interpreter, or database value is inferred. Port 4001
    is rejected because this repository's verified local setup documents it as
    the live Gateway endpoint.
    """

    python: str | Path
    runtime_cwd: str | Path
    gateway_host: str
    gateway_port: int
    paper_endpoint_profile: IbkrPaperEndpointProfile
    client_id: int
    account_ref: str
    database_path: str | Path
    lock_directory: str | Path
    recovery_timeout: float = 30.0
    snapshot_timeout: float = 30.0
    snapshot_freshness_seconds: float = 10.0
    stop_timeout: float = 10.0
    poll_interval: float = 0.05
    callback_poll_interval: float = 0.05
    health_poll_interval: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.gateway_host, str) or not self.gateway_host.strip():
            raise IbkrPaperRuntimeConfigurationError("an explicit Gateway host is required")
        gateway_host = self.gateway_host.strip().lower()
        if gateway_host not in _LOCAL_GATEWAY_HOSTS:
            raise IbkrPaperRuntimeConfigurationError("PAPER Gateway host must be an explicit loopback address")
        if type(self.gateway_port) is not int or not 1 <= self.gateway_port <= 65535:
            raise IbkrPaperRuntimeConfigurationError("gateway_port must be an integer from 1 to 65535")
        if not isinstance(self.paper_endpoint_profile, IbkrPaperEndpointProfile):
            raise IbkrPaperRuntimeConfigurationError("an explicit supported PAPER endpoint profile is required")
        if self.gateway_port in {4001, 7496}:
            raise IbkrPaperRuntimeConfigurationError("configured Gateway port is a known LIVE endpoint")
        expected_port = _PAPER_PROFILE_PORTS[self.paper_endpoint_profile]
        if self.gateway_port != expected_port:
            raise IbkrPaperRuntimeConfigurationError(
                f"{self.paper_endpoint_profile.value} requires its default PAPER port {expected_port}"
            )
        if type(self.client_id) is not int or self.client_id < 0:
            raise IbkrPaperRuntimeConfigurationError("client_id must be a non-negative integer")
        if not isinstance(self.account_ref, str) or not self.account_ref.strip():
            raise IbkrPaperRuntimeConfigurationError("an explicit PAPER account reference is required")
        account_ref = self.account_ref.strip().upper()
        if not _PAPER_ACCOUNT_RE.fullmatch(account_ref):
            raise IbkrPaperRuntimeConfigurationError(
                "single-user PAPER account reference must match the IBKR DU numeric account form"
            )
        python_path = Path(self.python).expanduser()
        cwd_path = Path(self.runtime_cwd).expanduser()
        if not python_path.is_file() or not cwd_path.is_dir():
            raise IbkrPaperRuntimeConfigurationError("the locked SDK Python and runtime directory must exist")
        database_path = Path(self.database_path).expanduser().resolve()
        lock_directory = Path(self.lock_directory).expanduser().resolve()
        if database_path.exists() and not database_path.is_file():
            raise IbkrPaperRuntimeConfigurationError("database_path must identify a file")
        if lock_directory.exists() and not lock_directory.is_dir():
            raise IbkrPaperRuntimeConfigurationError("lock_directory must identify a directory")
        if database_path == lock_directory or database_path in lock_directory.parents:
            raise IbkrPaperRuntimeConfigurationError("database and owner-lock paths must be separate")
        for name in (
            "recovery_timeout", "snapshot_timeout", "snapshot_freshness_seconds", "stop_timeout", "poll_interval",
            "callback_poll_interval", "health_poll_interval",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise IbkrPaperRuntimeConfigurationError(f"{name} must be positive")
        if not 1 <= self.snapshot_freshness_seconds <= 300:
            raise IbkrPaperRuntimeConfigurationError("snapshot_freshness_seconds must be between 1 and 300 seconds")
        object.__setattr__(self, "python", python_path.absolute())
        object.__setattr__(self, "runtime_cwd", cwd_path.resolve())
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "lock_directory", lock_directory)
        object.__setattr__(self, "gateway_host", self.gateway_host.strip())
        object.__setattr__(self, "account_ref", account_ref)


@dataclass(frozen=True, slots=True)
class IbkrPaperRuntimeDependencies:
    """Trusted application facts required before a runtime can be built.

    Callers must supply real host-side consumers and checks. In particular,
    there are no permissive default recovery, activation, or quiescence checks.
    """

    callback_sink: Callable[[Any], None]
    recovery_check: Callable[[], bool]
    activation_check: Callable[[], bool]
    quiesce_check: Callable[[], bool]
    intent_loader: Callable[[str], Any]
    contract_loader: Callable[[str], Any]

    def __post_init__(self) -> None:
        for name in (
            "callback_sink", "recovery_check", "activation_check", "quiesce_check",
            "intent_loader", "contract_loader",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be an explicitly supplied callable")


class IbkrPaperRuntimeFactory:
    """Create one fully wired managed runtime without starting its process."""

    def __init__(
        self,
        *,
        config: IbkrPaperRuntimeConfig,
        dependencies: IbkrPaperRuntimeDependencies,
        snapshot_id_factory: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(config, IbkrPaperRuntimeConfig):
            raise TypeError("config must be IbkrPaperRuntimeConfig")
        if not isinstance(dependencies, IbkrPaperRuntimeDependencies):
            raise TypeError("dependencies must be IbkrPaperRuntimeDependencies")
        if snapshot_id_factory is not None and not callable(snapshot_id_factory):
            raise TypeError("snapshot_id_factory must be callable or None")
        self.config = config
        self.dependencies = dependencies
        self.snapshot_id_factory = snapshot_id_factory

    def __call__(self, scope: PaperRuntimeScope) -> IbkrManagedPaperRuntime:
        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a PaperRuntimeScope")
        if scope.broker != "IBKR" or scope.environment is not Environment.PAPER:
            raise ValueError("factory accepts IBKR/PAPER scopes only")
        if scope.account_ref != self.config.account_ref:
            raise ValueError("requested account differs from the explicit PAPER runtime configuration")

        database_path = self.config.database_path
        ledger = SQLiteOrderLedger(database_path)
        order_index = BrokerOrderIndex(database_path)
        allocator = IbkrOrderIdAllocator(database_path)
        snapshot_store = SQLiteIbkrSnapshotStore(database_path)
        process = IbkrRuntimeProcess(
            python=self.config.python,
            cwd=self.config.runtime_cwd,
            spool_path=database_path,
        )
        service = PaperExecutionService(
            ledger=ledger,
            lock_directory=self.config.lock_directory,
            broker=scope.broker,
            account_ref=scope.account_ref,
            environment=scope.environment,
            owner_id=f"paper-runtime-{secrets.token_hex(16)}",
            recovery_check=self.dependencies.recovery_check,
            activation_check=self.dependencies.activation_check,
            quiesce_check=self.dependencies.quiesce_check,
        )
        callback_pump = IbkrCallbackPump(
            events=queue.Queue(),
            overflowed=threading.Event(),
            ledger=ledger,
            order_index=order_index,
            broker=scope.broker,
            account_ref=scope.account_ref,
            environment=scope.environment,
            callback_sink=self.dependencies.callback_sink,
            next_valid_id_sink=allocator.observe_next_valid_id,
            reconciliation_sink=snapshot_store.record,
        )
        callback_worker = IbkrCallbackPumpWorker(
            callback_pump,
            poll_interval=self.config.callback_poll_interval,
        )
        health_monitor = IbkrRuntimeHealthMonitor.for_process(
            process,
            pause=lambda reason: service.pause(reason=reason),
            poll_interval=self.config.health_poll_interval,
        )
        coordinator = IbkrReconciliationCoordinator(
            store=snapshot_store,
            process=process,
            account_ref=scope.account_ref,
        )
        reconciler = IbkrRecoveryReconciler(
            store=snapshot_store,
            ledger=ledger,
            order_index=order_index,
            allocator=allocator,
            account_ref=scope.account_ref,
        )
        runtime = IbkrManagedPaperRuntime(
            service=service,
            process=process,
            callback_pump=callback_pump,
            callback_worker=callback_worker,
            health_monitor=health_monitor,
            coordinator=coordinator,
            reconciler=reconciler,
            ledger=ledger,
            order_index=order_index,
            allocator=allocator,
            snapshot_store=snapshot_store,
            gateway_host=self.config.gateway_host,
            gateway_port=self.config.gateway_port,
            client_id=self.config.client_id,
            account_ref=scope.account_ref,
            recovery_timeout=self.config.recovery_timeout,
            snapshot_timeout=self.config.snapshot_timeout,
            stop_timeout=self.config.stop_timeout,
            poll_interval=self.config.poll_interval,
            snapshot_id_factory=self.snapshot_id_factory,
            snapshot_freshness_seconds=self.config.snapshot_freshness_seconds,
        )
        # These transports remain dormant until a trusted dispatch composition
        # explicitly consumes them after recovery and its own risk checks.
        runtime.paper_transport = IbkrSubprocessPaperTransport(
            process=process,
            intent_loader=self.dependencies.intent_loader,
            contract_loader=self.dependencies.contract_loader,
        )
        runtime.cancel_transport = IbkrSubprocessCancelTransport(process=process)
        return runtime


__all__ = [
    "IbkrPaperEndpointProfile",
    "IbkrPaperRuntimeConfigurationError",
    "IbkrPaperRuntimeConfig",
    "IbkrPaperRuntimeDependencies",
    "IbkrPaperRuntimeFactory",
]
