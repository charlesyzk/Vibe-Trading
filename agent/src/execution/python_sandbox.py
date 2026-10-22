"""Fail-closed Docker launcher for isolated Python strategy workers.

This module only starts a prebuilt worker image. It never evaluates uploaded
Python in the Vibe host process. The worker protocol sends one bounded JSON
request containing only the current released event and reads bounded JSON
output from stdout. It does not provide account access or a management channel.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any


_IMAGE_DIGEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
_MAX_CPU_COUNT = 8.0
_MAX_PIDS = 256
_MAX_TMPFS_BYTES = 1024 * 1024 * 1024


def validate_worker_image_identity(image: str) -> str:
    """Return a digest-pinned immutable Docker image identity or reject it."""
    if not isinstance(image, str) or not (_IMAGE_DIGEST_RE.fullmatch(image) or _IMAGE_ID_RE.fullmatch(image)):
        raise ValueError("image must be pinned by @sha256:<digest> or an immutable sha256:<image-id>")
    return image


class PythonSandboxError(RuntimeError):
    """Base error raised by the isolated Python worker launcher."""


class SandboxUnavailable(PythonSandboxError):
    """The configured Docker daemon or pinned image is unavailable."""


class SandboxTimeout(PythonSandboxError):
    """The worker exceeded its hard wall-clock timeout."""


class SandboxOutputLimit(PythonSandboxError):
    """The Docker CLI returned more than the configured output bound."""


class SandboxExecutionError(PythonSandboxError):
    """The worker failed or returned an invalid response."""


class SandboxStrategyFailure(PythonSandboxError):
    """A worker reported a bounded strategy error without user-controlled text."""

    def __init__(self, category: str, error_type: str, source_line: int | None) -> None:
        self.category = category
        self.error_type = error_type
        self.source_line = source_line
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource and protocol bounds for one worker invocation."""

    uid: int = 65532
    gid: int = 65532
    memory_bytes: int = 512 * 1024 * 1024
    cpu_count: float = 1.0
    pids_limit: int = 64
    tmpfs_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 30.0
    max_request_bytes: int = 1 * 1024 * 1024
    max_output_bytes: int = 1 * 1024 * 1024

    def __post_init__(self) -> None:
        _require_int("uid", self.uid, minimum=1, maximum=2**31 - 1)
        _require_int("gid", self.gid, minimum=1, maximum=2**31 - 1)
        _require_int("memory_bytes", self.memory_bytes, minimum=16 * 1024 * 1024, maximum=_MAX_MEMORY_BYTES)
        _require_number("cpu_count", self.cpu_count, minimum=0.1, maximum=_MAX_CPU_COUNT)
        _require_int("pids_limit", self.pids_limit, minimum=1, maximum=_MAX_PIDS)
        _require_int("tmpfs_bytes", self.tmpfs_bytes, minimum=1024 * 1024, maximum=_MAX_TMPFS_BYTES)
        _require_number("timeout_seconds", self.timeout_seconds, minimum=0.1, maximum=_MAX_TIMEOUT_SECONDS)
        _require_int("max_request_bytes", self.max_request_bytes, minimum=1, maximum=_MAX_REQUEST_BYTES)
        _require_int("max_output_bytes", self.max_output_bytes, minimum=1, maximum=_MAX_OUTPUT_BYTES)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Bounded response returned by the image's JSON-over-stdio protocol."""

    response: Any
    stdout: bytes
    stderr: bytes
    container_name: str


def _require_int(name: str, value: object, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _require_number(name: str, value: object, *, minimum: float, maximum: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}")


def _require_builtin_seccomp_profile(raw_options: bytes) -> None:
    """Fail closed unless Docker reports its active built-in seccomp profile.

    Container flags cannot make an unconfined daemon safe. This check is made
    on every worker invocation and accepts only Docker's built-in/default
    profile names; absent, malformed, custom, and unconfined configurations
    are not treated as equivalent security evidence.
    """
    try:
        options = json.loads(raw_options.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxUnavailable("Docker daemon seccomp options are missing or malformed") from exc
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise SandboxUnavailable("Docker daemon seccomp options are missing or malformed")

    profiles: list[str | None] = []
    for option in options:
        values: dict[str, str] = {}
        for part in option.split(","):
            key, separator, value = part.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        if values.get("name") == "seccomp":
            profiles.append(values.get("profile"))

    if len(profiles) != 1 or profiles[0] not in {"builtin", "default"}:
        raise SandboxUnavailable("Docker daemon must report its active built-in seccomp profile")


class PythonSandbox:
    """Run a JSON worker request in a digest-pinned Docker image.

    The image must already exist in the selected daemon. This launcher never
    pulls an image, passes host environment variables, mounts host paths, or
    invokes a shell. A deployment should supply a trusted worker image whose
    entrypoint implements the JSON request/response protocol.
    """

    def __init__(
        self,
        image: str,
        *,
        context: str | None = None,
        limits: SandboxLimits | None = None,
        preflight_timeout_seconds: float = 5.0,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        image = validate_worker_image_identity(image)
        context = context or os.environ.get("DOCKER_CONTEXT") or "colima-vibe-execution"
        if not isinstance(context, str) or not _CONTEXT_RE.fullmatch(context):
            raise ValueError("context must be a simple Docker context name")
        _require_number("preflight_timeout_seconds", preflight_timeout_seconds, minimum=0.1, maximum=30.0)
        _require_number("cleanup_timeout_seconds", cleanup_timeout_seconds, minimum=0.1, maximum=30.0)
        self.image = image
        self.context = context
        self.limits = limits or SandboxLimits()
        self.preflight_timeout_seconds = float(preflight_timeout_seconds)
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)

    def preflight(self) -> tuple[str, ...]:
        """Require a daemon and return the pinned image's configured command."""
        info = self._control(
            ["docker", "--context", self.context, "info", "--format", "{{.ServerVersion}}"],
            timeout=self.preflight_timeout_seconds,
            operation="Docker daemon preflight",
        )
        if not info.stdout.strip():
            raise SandboxUnavailable("Docker daemon preflight returned no server version")
        security = self._control(
            ["docker", "--context", self.context, "info", "--format", "{{json .SecurityOptions}}"],
            timeout=self.preflight_timeout_seconds,
            operation="Docker daemon seccomp preflight",
        )
        _require_builtin_seccomp_profile(security.stdout)
        inspect = self._control(
            [
                "docker",
                "--context",
                self.context,
                "image",
                "inspect",
                "--format",
                "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}",
                self.image,
            ],
            timeout=self.preflight_timeout_seconds,
            operation="pinned worker image preflight",
        )
        try:
            entrypoint_raw, command_raw = inspect.stdout.decode("utf-8").strip().split("|", 1)
            entrypoint = json.loads(entrypoint_raw)
            command = json.loads(command_raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxUnavailable("pinned worker image inspection returned an invalid command") from exc
        if entrypoint is None:
            entrypoint = []
        if command is None:
            command = []
        if not isinstance(entrypoint, list) or not isinstance(command, list):
            raise SandboxUnavailable("pinned worker image command must use exec-form arrays")
        worker_command = (*entrypoint, *command)
        if not worker_command or not all(isinstance(part, str) and part for part in worker_command):
            raise SandboxUnavailable("pinned worker image has no valid exec-form command")
        return worker_command

    def run_json(self, request: Any) -> SandboxResult:
        """Send one bounded JSON request to the isolated worker and parse JSON output."""
        try:
            request_bytes = json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("request must be JSON serializable without NaN or Infinity") from exc
        if len(request_bytes) > self.limits.max_request_bytes:
            raise ValueError("JSON request exceeds configured byte limit")

        # Preflight is deliberately before container creation and never pulls.
        worker_command = self.preflight()
        container_name = f"vibe-py-worker-{uuid.uuid4().hex}"
        argv = self._run_argv(container_name, worker_command)
        run_error: BaseException | None = None
        result: SandboxResult | None = None
        try:
            returncode, stdout, stderr = _communicate_bounded(
                argv,
                request_bytes,
                timeout=self.limits.timeout_seconds,
                max_output_bytes=self.limits.max_output_bytes,
            )
            if returncode == 124:
                raise SandboxTimeout(
                    f"worker exceeded its in-container {self.limits.timeout_seconds:g} second wall-clock timeout"
                )
            if returncode != 0:
                try:
                    error = json.loads(stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    error = None
                if isinstance(error, dict) and set(error) == {"worker_error", "error_type", "source_line"}:
                    categories = {"strategy_runtime_error", "strategy_contract_error"}
                    error_types = {
                        "AssertionError", "AttributeError", "IndexError", "KeyError", "NameError",
                        "OverflowError", "TypeError", "ValueError", "ZeroDivisionError", "OtherError",
                    }
                    line = error["source_line"]
                    if (
                        error["worker_error"] in categories
                        and error["error_type"] in error_types
                        and (line is None or (type(line) is int and 1 <= line <= 262_144))
                    ):
                        raise SandboxStrategyFailure(error["worker_error"], error["error_type"], line)
                message = stderr.decode("utf-8", errors="replace")[:1000]
                raise SandboxExecutionError(f"worker container exited with status {returncode}: {message}")
            try:
                response = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SandboxExecutionError("worker stdout must contain exactly one JSON response") from exc
            result = SandboxResult(response, stdout, stderr, container_name)
        except BaseException as exc:
            run_error = exc
        cleanup_error: BaseException | None = None
        try:
            self._remove_container(container_name)
        except BaseException as exc:
            cleanup_error = exc
        if run_error is not None:
            if cleanup_error is not None:
                raise PythonSandboxError(f"{run_error}; cleanup also failed: {cleanup_error}") from run_error
            raise run_error
        if cleanup_error is not None:
            raise PythonSandboxError(f"worker completed but container cleanup failed: {cleanup_error}") from cleanup_error
        assert result is not None
        return result

    def _run_argv(self, container_name: str, worker_command: tuple[str, ...]) -> list[str]:
        limits = self.limits
        tmpfs = f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes},mode=1777"
        work_tmpfs = f"/work:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes},mode=1777"
        return [
            "docker",
            "--context",
            self.context,
            "run",
            "--interactive",
            # The daemon owns the container lifetime. --rm makes an internally
            # timed-out container disappear even if this host process is killed.
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--user",
            f"{limits.uid}:{limits.gid}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            # Explicitly request Docker's built-in profile even when a daemon
            # has a custom or disabled default. preflight independently checks
            # the active daemon configuration before the container is created.
            "--security-opt",
            "seccomp=builtin",
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            f"{limits.cpu_count:g}",
            "--tmpfs",
            tmpfs,
            "--tmpfs",
            work_tmpfs,
            # GNU timeout runs as PID 1 in the container. Its deadline is
            # passed as an argv value, so uploaded code cannot change it via
            # environment or request data. When PID 1 exits, the kernel tears
            # down any remaining processes in the container's PID namespace.
            "--entrypoint",
            "/usr/bin/timeout",
            self.image,
            "--kill-after=2s",
            "--signal=TERM",
            f"{limits.timeout_seconds:g}s",
            *worker_command,
        ]

    def _remove_container(self, container_name: str) -> None:
        """Remove a worker container; treat auto-removal as successful cleanup."""
        try:
            self._control(
                ["docker", "--context", self.context, "rm", "--force", "--volumes", container_name],
                timeout=self.cleanup_timeout_seconds,
                operation="worker container cleanup",
            )
        except SandboxUnavailable as exc:
            # --rm can race this normal-path cleanup. A daemon-confirmed missing
            # container is already the desired state; connection and other
            # daemon errors must still fail closed.
            if "no such container" not in str(exc).lower():
                raise

    def _control(self, argv: list[str], *, timeout: float, operation: str) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable(f"{operation} failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[:1000]
            raise SandboxUnavailable(f"{operation} failed: {detail}")
        return completed


def _communicate_bounded(
    argv: list[str],
    input_bytes: bytes,
    *,
    timeout: float,
    max_output_bytes: int,
) -> tuple[int, bytes, bytes]:
    """Pipe a request while bounding combined stdout/stderr and wall time."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        raise SandboxUnavailable(f"could not start Docker CLI: {exc}") from exc

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    input_view = memoryview(input_bytes)
    input_offset = 0
    stdout = bytearray()
    stderr = bytearray()
    output_total = 0
    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    for stream in (process.stdin, process.stdout, process.stderr):
        # os.set_blocking is available on supported Python 3.11+ platforms.
        import os

        os.set_blocking(stream.fileno(), False)

    deadline = time.monotonic() + timeout
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxTimeout(f"worker exceeded {timeout:g} second wall-clock timeout")
            if not selector.get_map():
                time.sleep(min(0.01, remaining))
                continue
            for key, _ in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                kind = key.data
                if kind == "stdin":
                    if input_offset >= len(input_view):
                        selector.unregister(stream)
                        stream.close()
                        continue
                    try:
                        written = stream.write(input_view[input_offset : input_offset + 65536])
                    except BrokenPipeError:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if written:
                        input_offset += written
                else:
                    chunk = stream.read(65536)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    output_total += len(chunk)
                    if output_total > max_output_bytes:
                        raise SandboxOutputLimit(f"worker output exceeded {max_output_bytes} byte limit")
                    (stdout if kind == "stdout" else stderr).extend(chunk)
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return returncode, bytes(stdout), bytes(stderr)
    except subprocess.TimeoutExpired as exc:
        raise SandboxTimeout(f"worker exceeded {timeout:g} second wall-clock timeout") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
