"""Explicit lifecycle owner for the official IBKR API client.

Importing this module and constructing an owner never opens a socket. Explicit
``start`` queues connection and the blocking SDK loop on one dedicated thread,
so the parent control loop remains responsive if socket connection stalls.
"""

from __future__ import annotations

from enum import Enum
import math
from queue import Queue
from threading import Event, Lock, Thread
from typing import Callable

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from .callbacks import CallbackEvent, IbapiCallbackAdapter, SQLiteCallbackSpool


class ClientLifecycle(str, Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


ClientFactory = Callable[[EWrapper], EClient]
ThreadFactory = Callable[..., Thread]


def official_client_factory(wrapper: EWrapper) -> EClient:
    """Build the official SDK client without connecting it."""
    return EClient(wrapper)


class IbapiClientOwner:
    """Own one SDK client, callback queue, durable spool, and run thread."""

    def __init__(
        self,
        spool_path: str,
        *,
        queue_capacity: int = 1024,
        max_event_bytes: int = 64 * 1024,
        client_factory: ClientFactory = official_client_factory,
        thread_factory: ThreadFactory = Thread,
    ) -> None:
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if not callable(client_factory) or not callable(thread_factory):
            raise TypeError("client_factory and thread_factory must be callable")
        self.events: Queue[CallbackEvent] = Queue(maxsize=queue_capacity)
        self.overflowed = Event()
        self.spool = SQLiteCallbackSpool(spool_path, max_event_bytes=max_event_bytes)
        self.adapter = IbapiCallbackAdapter(
            self.events, self.overflowed, durable_sink=self.spool.append
        )
        self.client = client_factory(self.adapter)
        self.adapter.cancel_account_summary = self.client.cancelAccountSummary
        self.adapter.cancel_positions = self.client.cancelPositions
        self._thread_factory = thread_factory
        self._thread: Thread | None = None
        self._lock = Lock()
        self._state = ClientLifecycle.NEW
        self._run_error: BaseException | None = None
        self._disconnect_error: BaseException | None = None
        self._run_finished = Event()

    @property
    def state(self) -> ClientLifecycle:
        with self._lock:
            return self._state

    @property
    def run_error(self) -> BaseException | None:
        with self._lock:
            return self._run_error

    @property
    def thread(self) -> Thread | None:
        with self._lock:
            return self._thread

    def start(self, host: str, port: int, client_id: int) -> None:
        """Start connection asynchronously on the dedicated SDK thread."""
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        if type(client_id) is not int or client_id < 0:
            raise ValueError("client_id must be a non-negative integer")

        with self._lock:
            if self._state is not ClientLifecycle.NEW:
                raise RuntimeError(f"client cannot start from {self._state.value}")
            self._state = ClientLifecycle.STARTING
            try:
                thread = self._thread_factory(
                    target=self._connect_and_run,
                    args=(host.strip(), port, client_id),
                    name=f"ibapi-run-{client_id}",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
            except BaseException as exc:
                self._run_error = exc
                self._state = ClientLifecycle.FAILED
                raise

    def _connect_and_run(self, host: str, port: int, client_id: int) -> None:
        try:
            # The SDK spelling is clientId in the official EClient API.
            self.client.connect(host, port, clientId=client_id)
            with self._lock:
                if self._state is ClientLifecycle.STOPPING:
                    # stop() raced with a still-blocking connect. Do not start
                    # the event loop after the caller requested disconnect.
                    return
                self._state = ClientLifecycle.RUNNING
            self.client.run()
        except BaseException as exc:
            with self._lock:
                self._run_error = exc
                self._state = ClientLifecycle.FAILED
        else:
            with self._lock:
                if self._state not in (ClientLifecycle.STOPPING, ClientLifecycle.STOPPED):
                    self._run_error = RuntimeError("IBKR SDK run loop exited unexpectedly")
                    self._state = ClientLifecycle.FAILED
        finally:
            self._run_finished.set()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Disconnect and join within ``timeout``; raise if the loop survives it."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative number")
        with self._lock:
            if self._state is ClientLifecycle.NEW:
                self._state = ClientLifecycle.STOPPED
                return
            if self._state is ClientLifecycle.STOPPED:
                return
            thread = self._thread
            self._state = ClientLifecycle.STOPPING

        try:
            self.client.disconnect()
        except BaseException as exc:
            with self._lock:
                self._disconnect_error = exc
        else:
            with self._lock:
                self._disconnect_error = None

        if thread is not None:
            thread.join(timeout=float(timeout))
            if thread.is_alive():
                # Keep STOPPING so the caller can retry the bounded join later.
                raise TimeoutError("IBKR SDK run thread did not stop before timeout")

        with self._lock:
            disconnect_error = self._disconnect_error
            run_error = self._run_error
            if disconnect_error is not None or run_error is not None:
                self._state = ClientLifecycle.FAILED
            else:
                self._state = ClientLifecycle.STOPPED
        if disconnect_error is not None:
            raise RuntimeError("IBKR client disconnect failed") from disconnect_error
        if run_error is not None:
            raise RuntimeError("IBKR SDK run loop failed; broker reconciliation is required") from run_error

    def confirm_reconciled_stop(self) -> None:
        """Release a FAILED process lifecycle only after host broker reconciliation."""
        with self._lock:
            if self._state is not ClientLifecycle.FAILED:
                raise RuntimeError(f"reconciliation confirmation is invalid from {self._state.value}")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("SDK thread is still alive")
            if self._disconnect_error is not None:
                raise RuntimeError("disconnect failure must be resolved before reconciliation confirmation")
            self._state = ClientLifecycle.STOPPED

    def request_reconciliation_snapshot(self, snapshot_id: int) -> int:
        """Request account, position, and all-client open-order snapshots.

        The returned ID is a correlation token only. The host must wait for
        all three durable end markers and reconcile the rows against its own
        ledger before it can enable any order-ID or dispatch gate.
        """
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ValueError("snapshot_id must be a positive integer")
        with self._lock:
            if self._state is not ClientLifecycle.RUNNING:
                raise RuntimeError("IBKR SDK client is not running")
        self.adapter.begin_reconciliation_snapshot(snapshot_id)
        self.client.reqAccountSummary(
            snapshot_id, "All", "NetLiquidation,TotalCashValue,AvailableFunds"
        )
        self.client.reqPositions()
        self.client.reqAllOpenOrders()
        return snapshot_id

    def request_order_ids(self) -> None:
        """Request a fresh nextValidId without changing the connection epoch."""
        with self._lock:
            if self._state is not ClientLifecycle.RUNNING:
                raise RuntimeError("IBKR SDK client is not running")
            if not self.adapter.api_ready:
                raise RuntimeError("IBKR SDK client is not API-ready")
        # EClient.reqIds invokes EWrapper.nextValidId asynchronously on the
        # reader thread; the trusted parent waits before reconciliation.
        self.client.reqIds(-1)
