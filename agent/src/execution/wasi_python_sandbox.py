"""Digest-pinned WASI alternative for the isolated strategy event worker."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import sys
from pathlib import Path
from typing import Any

from src.execution.python_sandbox import (
    PythonSandbox, SandboxExecutionError, SandboxLimits, SandboxResult, SandboxStrategyFailure,
    SandboxTimeout, SandboxUnavailable, _communicate_bounded,
)

WASM_SHA256 = "5ce0cbeb843e6e5abf2d50c7158002e8333c26a40fbe27a7a52e66ee48cf64a8"
STDLIB_SHA256 = "74130c400ba5b818bf58bfc2f41fc075f4350cbb13e53099c84e9c0494ec5444"
WORKER_SHA256 = "f3d414638e35640137f9e5e1ea5e92d33398b3ff4c8defad0de21b3e0fe60107"
HOST_SHA256 = "57a79cdbdb1b6eb24a89f6767858ed6216a862ec5e0189da63f7cb3b30622f3e"
WASI_WORKER_ID = "sha256:" + hashlib.sha256(
    f"wasi-python-3.12.0|wasmtime-29.0.0|{WASM_SHA256}|{STDLIB_SHA256}|{WORKER_SHA256}|{HOST_SHA256}".encode()
).hexdigest()


class WasiPythonSandbox(PythonSandbox):
    """Run the existing one-event worker in a capability-limited WASI guest."""

    def __init__(self, runtime_root: str | Path, *, limits: SandboxLimits | None = None) -> None:
        self.root = Path(runtime_root).resolve()
        self.limits = limits or SandboxLimits()
        self.image = WASI_WORKER_ID
        self._verify_resources()

    def _verify_resources(self) -> None:
        try:
            if metadata.version("wasmtime") != "29.0.0":
                raise SandboxUnavailable("WASI runtime version differs from the pinned version")
        except metadata.PackageNotFoundError as exc:
            raise SandboxUnavailable("WASI runtime dependency is unavailable") from exc
        wasm = self.root / "python.wasm"
        stdlib = self.root / "stdlib"
        worker = Path(__file__).resolve().parent / "strategy_worker_image" / "worker.py"
        host = Path(__file__).resolve().parent / "wasi_worker_host.py"
        if stdlib.is_symlink() or not stdlib.is_dir() or {item.name for item in stdlib.iterdir()} != {"python312.zip"}:
            raise SandboxUnavailable("WASI standard library directory is invalid")
        for file, expected in ((wasm, WASM_SHA256), (stdlib / "python312.zip", STDLIB_SHA256),
                               (worker, WORKER_SHA256), (host, HOST_SHA256)):
            if file.is_symlink() or not file.is_file():
                raise SandboxUnavailable("WASI worker resource is unavailable")
            with file.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise SandboxUnavailable("WASI worker resource does not match its pinned digest")

    def run_json(self, request: Any) -> SandboxResult:
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("request must be JSON serializable without NaN or Infinity") from exc
        if len(encoded) > self.limits.max_request_bytes:
            raise ValueError("JSON request exceeds configured byte limit")
        self._verify_resources()
        host = Path(__file__).resolve().parent / "wasi_worker_host.py"
        code, stdout, stderr = _communicate_bounded(
            [sys.executable, "-I", str(host), str(self.root / "python.wasm"), str(self.root / "stdlib"),
             str(self.limits.memory_bytes)], encoded,
            timeout=self.limits.timeout_seconds, max_output_bytes=self.limits.max_output_bytes,
        )
        if code == 124:
            raise SandboxTimeout("WASI worker exceeded its execution limits")
        if code != 0:
            try:
                error = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error = None
            if isinstance(error, dict) and set(error) == {"worker_error", "error_type", "source_line"}:
                category, kind, line = error["worker_error"], error["error_type"], error["source_line"]
                if (category in {"strategy_runtime_error", "strategy_contract_error"}
                        and kind in {"AssertionError", "AttributeError", "IndexError", "KeyError", "NameError",
                                     "OverflowError", "TypeError", "ValueError", "ZeroDivisionError", "OtherError"}
                        and (line is None or (type(line) is int and 1 <= line <= 262144))):
                    raise SandboxStrategyFailure(category, kind, line)
            raise SandboxExecutionError(f"WASI worker exited with status {code}")
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxExecutionError("WASI worker stdout must contain exactly one JSON response") from exc
        return SandboxResult(response=response, stdout=stdout, stderr=stderr, container_name="wasi")
