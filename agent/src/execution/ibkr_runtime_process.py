"""Parent-side supervisor for a dedicated official-ibapi Python subprocess."""

from __future__ import annotations

import os
import json
import math
from pathlib import Path
import queue
import secrets
import subprocess
import threading
from typing import Any, BinaryIO, Callable, Mapping

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
_COMMAND_FIELDS: dict[str, set[str]] = {
    "status": set(), "start": {"host", "port", "client_id"},
    "stop": {"timeout"}, "shutdown": set(), "confirm_reconciled_stop": set(),
    "request_order_ids": set(),
    "request_reconciliation": {"snapshot_id"},
    "place_order": {"broker_order_id", "environment", "contract", "order"},
    "cancel_order": {"broker_order_id", "environment"},
}


class RuntimeIpcError(ValueError):
    """Malformed or unsupported frame in the versioned runtime protocol."""


def _encode_frame(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeIpcError("frame is not finite JSON") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise RuntimeIpcError("frame exceeds byte limit")
    return raw + b"\n"


def _read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if raw == b"":
        return None
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise RuntimeIpcError("frame exceeds byte limit or is unterminated")
    try:
        value = json.loads(raw[:-1].decode("ascii"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeIpcError("frame is not valid finite JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeIpcError("frame must be an object")
    return value


def validate_command(command: str, payload: dict[str, Any]) -> None:
    if not isinstance(command, str) or command not in _COMMAND_FIELDS or set(payload) != _COMMAND_FIELDS[command]:
        raise RuntimeIpcError("unsupported command or payload fields")
    if command == "start":
        if not isinstance(payload["host"], str) or not payload["host"].strip() or len(payload["host"]) > 255:
            raise RuntimeIpcError("invalid host")
        if type(payload["port"]) is not int or not 1 <= payload["port"] <= 65535:
            raise RuntimeIpcError("invalid port")
        if type(payload["client_id"]) is not int or payload["client_id"] < 0:
            raise RuntimeIpcError("invalid client id")
    if command == "stop":
        timeout = payload["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise RuntimeIpcError("invalid stop timeout")
    if command == "request_reconciliation":
        if type(payload["snapshot_id"]) is not int or payload["snapshot_id"] <= 0:
            raise RuntimeIpcError("snapshot_id must be a positive integer")
    if command in {"place_order", "cancel_order"}:
        if type(payload["broker_order_id"]) is not int or payload["broker_order_id"] <= 0:
            raise RuntimeIpcError("broker order id must be a positive integer")
        if payload["environment"] != "paper":
            raise RuntimeIpcError("runtime accepts PAPER commands only")
    if command == "place_order":
        contract, order = payload["contract"], payload["order"]
        contract_fields = {"con_id", "symbol", "security_type", "exchange", "primary_exchange", "currency"}
        order_fields = {"action", "quantity", "order_type", "time_in_force", "account_ref", "client_order_id", "outside_rth", "transmit"}
        if not isinstance(contract, dict) or set(contract) != contract_fields:
            raise RuntimeIpcError("contract payload has unknown or missing fields")
        if not isinstance(order, dict) or set(order) != order_fields:
            raise RuntimeIpcError("order payload has unknown or missing fields")
        if type(contract["con_id"]) is not int or contract["con_id"] <= 0:
            raise RuntimeIpcError("contract conId must be a positive integer")
        for key in ("symbol", "primary_exchange"):
            if not isinstance(contract[key], str) or not contract[key].strip() or len(contract[key]) > 32:
                raise RuntimeIpcError(f"invalid contract {key}")
        if (
            contract["security_type"] != "STK"
            or contract["exchange"] != "SMART"
            or contract["currency"] != "USD"
        ):
            raise RuntimeIpcError("only SMART/USD/STK contracts are accepted")
        if (
            not isinstance(order["action"], str)
            or order["action"] not in {"BUY", "SELL"}
            or type(order["quantity"]) is not int
            or order["quantity"] <= 0
        ):
            raise RuntimeIpcError("order must have BUY/SELL and positive whole-share quantity")
        if order["order_type"] != "MKT" or order["time_in_force"] != "DAY":
            raise RuntimeIpcError("only MKT/DAY orders are accepted")
        if not isinstance(order["account_ref"], str) or not order["account_ref"].strip() or len(order["account_ref"]) > 128:
            raise RuntimeIpcError("configured account reference is required")
        if not isinstance(order["client_order_id"], str) or not order["client_order_id"].strip() or len(order["client_order_id"]) > 64:
            raise RuntimeIpcError("client order reference is required")
        if type(order["outside_rth"]) is not bool or order["outside_rth"] is not False:
            raise RuntimeIpcError("outside regular trading hours is not supported")
        if type(order["transmit"]) is not bool or order["transmit"] is not True:
            raise RuntimeIpcError("transmit must be enabled for an authorized submission")


def _validate_response(value: dict[str, Any], request_id: str) -> dict[str, Any]:
    if set(value) != {"protocol", "type", "request_id", "ok", "result", "error"}:
        raise RuntimeIpcError("response has unknown or missing fields")
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL_VERSION or value["type"] != "response":
        raise RuntimeIpcError("unsupported response protocol")
    if value["request_id"] != request_id or type(value["ok"]) is not bool:
        raise RuntimeIpcError("response identity mismatch")
    if not isinstance(value["result"], dict):
        raise RuntimeIpcError("response result must be an object")
    if value["ok"] and value["error"] is not None:
        raise RuntimeIpcError("successful response has error")
    if not value["ok"] and (not isinstance(value["error"], str) or not value["error"]):
        raise RuntimeIpcError("failed response has no error")
    return value


class RuntimeProcessError(RuntimeError):
    """The isolated IBKR process failed or violated its control protocol."""


class IbkrRuntimeProcess:
    """Own one locked-interpreter child and its bounded stdio control channel.

    The child starts disconnected. The caller must explicitly issue ``start``.
    A broken pipe, timeout, malformed frame, or child exit is treated as an
    uncertain lifecycle result and must be reconciled before another owner is
    started.
    """

    def __init__(self, *, python: str | Path, cwd: str | Path, spool_path: str | Path,
                 env: Mapping[str, str] | None = None, request_timeout: float = 10.0) -> None:
        # Preserve a venv's executable symlink: Python locates pyvenv.cfg next
        # to the invoked path, so resolving it can escape the locked runtime.
        python_path = Path(os.path.abspath(Path(python).expanduser()))
        cwd_path = Path(cwd).expanduser().resolve()
        if not python_path.is_file() or not cwd_path.is_dir():
            raise ValueError("python and runtime cwd must exist")
        if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.python = python_path
        self.cwd = cwd_path
        self.spool_path = str(Path(spool_path).expanduser().resolve())
        self.request_timeout = float(request_timeout)
        self._env = dict(env) if env is not None else self._minimal_environment()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue(maxsize=8)
        self._stderr = bytearray()
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = False
        self._failed = False

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME")
        return {key: os.environ[key] for key in allowed if key in os.environ}

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    def start_process(self) -> dict[str, Any]:
        """Launch the child without connecting and return its initial status."""
        with self._lock:
            if self._process is not None or self._closed:
                raise RuntimeProcessError("runtime process cannot be started in this state")
            process = subprocess.Popen(
                [str(self.python), "-m", "ibkr_runtime.worker", "--spool", self.spool_path],
                cwd=str(self.cwd), env=self._env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                close_fds=True,
            )
            self._process = process
            self._reader = threading.Thread(target=self._read_responses, name="ibkr-ipc-reader", daemon=True)
            self._stderr_reader = threading.Thread(target=self._drain_stderr, name="ibkr-stderr-drain", daemon=True)
            self._reader.start()
            self._stderr_reader.start()
        return self.request("status")

    def request(self, command: str, **payload: Any) -> dict[str, Any]:
        """Issue one strict command and wait for its correlated response."""
        with self._request_lock:
            with self._lock:
                process = self._process
                if process is None or process.poll() is not None or self._closed or self._failed:
                    raise RuntimeProcessError("runtime process is unavailable")
                validate_command(command, payload)
                request_id = secrets.token_urlsafe(24)
                frame = {"protocol": PROTOCOL_VERSION, "type": "command", "request_id": request_id,
                         "command": command, "payload": payload}
                try:
                    assert process.stdin is not None
                    process.stdin.write(_encode_frame(frame))
                    process.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    self._failed = True
                    raise RuntimeProcessError("runtime command pipe failed") from exc
            try:
                item = self._responses.get(timeout=self.request_timeout)
            except queue.Empty as exc:
                self._failed = True
                raise TimeoutError("runtime subprocess command timed out; lifecycle is uncertain") from exc
            if item is None:
                self._failed = True
                raise RuntimeProcessError("runtime subprocess closed its response stream")
            if isinstance(item, BaseException):
                self._failed = True
                raise RuntimeProcessError("runtime subprocess violated its IPC protocol") from item
            try:
                response = _validate_response(item, request_id)
            except Exception:
                self._failed = True
                raise
            if not response["ok"]:
                raise RuntimeProcessError(f"runtime rejected command: {response['error']}")
            return response["result"]

    def place_order(self, *, broker_order_id: int, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        """Submit one precommitted PAPER order through the isolated SDK process."""
        return self.request("place_order", broker_order_id=broker_order_id, environment="paper",
                            contract=contract, order=order)

    def cancel_order(self, broker_order_id: int) -> dict[str, Any]:
        """Cancel one order already authorized and marked pending by the host ledger."""
        return self.request("cancel_order", broker_order_id=broker_order_id, environment="paper")

    def confirm_reconciled_stop(self) -> dict[str, Any]:
        """Acknowledge a FAILED SDK lifecycle only after host recovery checks pass."""
        return self.request("confirm_reconciled_stop")

    def request_reconciliation(self, snapshot_id: int) -> int:
        """Request account, positions and all-client open orders from the SDK child."""
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise RuntimeIpcError("snapshot_id must be a positive integer")
        result = self.request("request_reconciliation", snapshot_id=snapshot_id)
        if (
            not isinstance(result, dict)
            or set(result) != {"snapshot_id"}
            or type(result["snapshot_id"]) is not int
            or result["snapshot_id"] != snapshot_id
        ):
            self._failed = True
            raise RuntimeIpcError("runtime returned an invalid reconciliation snapshot id")
        return result["snapshot_id"]

    def request_order_ids(self) -> None:
        """Ask the connected SDK client for a fresh nextValidId callback."""
        result = self.request("request_order_ids")
        if result != {"requested": True}:
            self._failed = True
            raise RuntimeIpcError("runtime returned an invalid order-id request result")

    def _read_responses(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                frame = _read_frame(process.stdout)
                if frame is None:
                    self._responses.put(None)
                    return
                self._responses.put(frame, timeout=1.0)
        except BaseException as exc:
            try:
                self._responses.put(exc, timeout=1.0)
            except queue.Full:
                pass

    def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            chunk = process.stderr.read(1024)
            if not chunk:
                return
            # Bound retained diagnostic output; never include it in IPC replies.
            self._stderr.extend(chunk)
            if len(self._stderr) > 8192:
                del self._stderr[:-8192]

    def close(self, *, timeout: float = 3.0) -> None:
        """Disconnect/join the child, then shut it down boundedly.

        The caller must first prove broker quiescence. If the SDK loop failed,
        ``stop`` retains FAILED and close raises until the trusted host has
        reconciled and called ``confirm_reconciled_stop``.
        """
        process = self._process
        if process is None:
            self._closed = True
            return
        if process.poll() is None:
            status = self.request("status")
            if status["state"] not in {"NEW", "STOPPED"}:
                self.request("stop", timeout=min(float(timeout), 30.0))
        self.request("shutdown")
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("runtime subprocess did not exit after shutdown") from exc
        if process.returncode != 0:
            raise RuntimeProcessError(f"runtime subprocess exited with code {process.returncode}")
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=1.0)
        self._closed = True


class IbkrSubprocessPaperTransport:
    """Serialize one host-authorized ledger intent for the SDK child.

    The contract loader must return a trusted, freshly verified mapping with
    ``con_id``, ``symbol``, ``security_type``, ``exchange``,
    ``primary_exchange`` and ``currency`` fields. Identity, grant, reservation,
    risk, broker ID binding, SEND_STARTED and attempt persistence remain the
    caller's responsibility and must be committed before this callable runs.
    """

    def __init__(self, *, process: IbkrRuntimeProcess, intent_loader: Any, contract_loader: Any) -> None:
        if not callable(intent_loader) or not callable(contract_loader):
            raise TypeError("intent_loader and contract_loader must be callable")
        self.process = process
        self.intent_loader = intent_loader
        self.contract_loader = contract_loader

    def __call__(self, command: Any, *, intent_id: str, broker_order_id: int,
                 attempt_id: str, client_order_id: str) -> None:
        intent = self.intent_loader(intent_id)
        environment = getattr(getattr(intent, "environment", None), "value", getattr(intent, "environment", None))
        if (
            getattr(intent, "broker", None) != "IBKR"
            or environment != "paper"
            or getattr(intent, "instrument_id", None) != command.instrument
            or getattr(intent, "side", None) != command.side
            or getattr(intent, "integer_qty", None) != command.quantity
            or getattr(intent, "order_type", None) != "market"
            or getattr(intent, "time_in_force", None) != "DAY"
            or not isinstance(intent_id, str) or not intent_id
            or not isinstance(attempt_id, str) or not attempt_id
            or not isinstance(client_order_id, str) or not client_order_id
        ):
            raise RuntimeIpcError("native order differs from the frozen PAPER intent")
        verified = self.contract_loader(intent.instrument_id)
        if isinstance(verified, Mapping):
            contract = dict(verified)
        else:
            contract = {
                "con_id": getattr(verified, "con_id", None),
                "symbol": getattr(verified, "symbol", None),
                "security_type": getattr(verified, "security_type", None),
                "exchange": getattr(verified, "exchange", None),
                "primary_exchange": getattr(verified, "primary_exchange", None),
                "currency": getattr(verified, "currency", None),
            }
        if (
            type(contract.get("con_id")) is not int or contract["con_id"] <= 0
            or contract.get("symbol") != intent.instrument_id
            or contract.get("security_type") != "STK"
            or contract.get("exchange") != "SMART"
            or contract.get("currency") != "USD"
            or not isinstance(contract.get("primary_exchange"), str)
            or not contract["primary_exchange"].strip()
        ):
            raise RuntimeIpcError("trusted contract mapping differs from the frozen instrument or PAPER scope")
        account_ref = getattr(intent, "account_ref", None)
        if not isinstance(account_ref, str) or not account_ref.strip():
            raise RuntimeIpcError("frozen PAPER intent has no configured account reference")
        order = {
            "action": command.side,
            "quantity": command.quantity,
            "order_type": "MKT",
            "time_in_force": "DAY",
            "account_ref": account_ref,
            "client_order_id": client_order_id,
            "outside_rth": False,
            "transmit": True,
        }
        self.process.place_order(broker_order_id=broker_order_id, contract=contract, order=order)


class IbkrSubprocessCancelTransport:
    """Route only an already-owned positive broker id to the child SDK client."""

    def __init__(self, *, process: IbkrRuntimeProcess) -> None:
        self.process = process

    def __call__(self, broker_order_id: int) -> None:
        if type(broker_order_id) is not int or broker_order_id <= 0:
            raise RuntimeIpcError("broker order id must be a positive integer")
        self.process.cancel_order(broker_order_id)


class IbkrRuntimeHealthMonitor:
    """Latch a durable pause when an expected-running SDK child is unhealthy.

    The monitor deliberately does not infer dispatch readiness from SDK
    handshake state. In particular, ``ready_for_dispatch=False`` is expected
    until full broker/account/order reconciliation is implemented.
    """

    def __init__(
        self,
        *,
        status_reader: Callable[[], dict[str, Any]],
        pause: Callable[[str], None],
        poll_interval: float = 1.0,
    ) -> None:
        if not callable(status_reader) or not callable(pause):
            raise TypeError("status_reader and pause must be callable")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not 0.05 <= poll_interval <= 60:
            raise ValueError("poll_interval must be between 50 ms and 60 seconds")
        self.status_reader = status_reader
        self.pause = pause
        self.poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._paused = threading.Event()

    @classmethod
    def for_process(
        cls,
        process: IbkrRuntimeProcess,
        *,
        pause: Callable[[str], None],
        poll_interval: float = 1.0,
    ) -> "IbkrRuntimeHealthMonitor":
        return cls(
            status_reader=lambda: process.request("status"),
            pause=pause,
            poll_interval=poll_interval,
        )

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def check_once(self) -> bool:
        """Check one status snapshot; return true only for a healthy runtime."""
        if self._paused.is_set():
            return False
        try:
            status = self.status_reader()
            if not isinstance(status, dict):
                raise RuntimeProcessError("runtime status is not an object")
            reason = self._unhealthy_reason(status)
            if reason is None:
                return True
            self._pause_once(reason)
            return False
        except BaseException as exc:
            self._failure = exc
            self._pause_once("ibkr_runtime_status_unavailable")
            return False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("health monitor cannot be started twice")
        self._thread = threading.Thread(
            target=self._run, name="ibkr-runtime-health", daemon=False,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=float(timeout))
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set() and not self._paused.is_set():
            self.check_once()
            self._stop.wait(self.poll_interval)

    @staticmethod
    def _unhealthy_reason(status: dict[str, Any]) -> str | None:
        if status.get("state") != "RUNNING":
            return "ibkr_runtime_not_running"
        if status.get("socket_connected") is not True:
            return "ibkr_socket_disconnected"
        if status.get("callback_overflow") is not False:
            return "ibkr_callback_overflow_or_unknown"
        if status.get("run_error") is not None:
            return "ibkr_sdk_run_loop_failed"
        if status.get("sdk_api_ready") is not True:
            return "ibkr_sdk_api_handshake_incomplete"
        return None

    def _pause_once(self, reason: str) -> None:
        if self._paused.is_set():
            return
        self._paused.set()
        try:
            self.pause(reason)
        except BaseException as exc:
            if self._failure is None:
                self._failure = exc
