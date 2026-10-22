"""Unit and opt-in runtime probes for the Docker strategy worker launcher."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from src.execution import python_sandbox
from src.execution.python_sandbox import (
    PythonSandbox,
    PythonSandboxError,
    SandboxLimits,
    SandboxOutputLimit,
    SandboxTimeout,
    SandboxUnavailable,
)


IMAGE = "registry.example.invalid/vibe/python-worker@sha256:" + "a" * 64


def _success(argv=None, **_kwargs):
    if argv and "info" in argv:
        if "SecurityOptions" in argv[-1]:
            return subprocess.CompletedProcess(
                [], 0, stdout=b'["name=seccomp,profile=builtin","name=cgroupns"]\n', stderr=b""
            )
        return subprocess.CompletedProcess([], 0, stdout=b"27.2.0\n", stderr=b"")
    if argv and "rm" in argv:
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    return subprocess.CompletedProcess(
        [], 0, stdout=b'["python","-I","/opt/test-worker.py"]|null\n', stderr=b""
    )


def test_worker_image_must_be_digest_pinned():
    for image in (
        "python:3.12",
        "registry.example.invalid/worker@sha256:" + "A" * 64,
        "registry.example.invalid/worker@@sha256:" + "a" * 64,
    ):
        with pytest.raises(ValueError, match="pinned"):
            PythonSandbox(image)
    assert PythonSandbox("sha256:" + "b" * 64).image == "sha256:" + "b" * 64


def test_resource_limits_reject_root_unbounded_and_boolean_values():
    with pytest.raises(ValueError, match="uid"):
        SandboxLimits(uid=0)
    with pytest.raises(ValueError, match="pids_limit"):
        SandboxLimits(pids_limit=True)
    with pytest.raises(ValueError, match="timeout_seconds"):
        SandboxLimits(timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="memory_bytes"):
        SandboxLimits(memory_bytes=9 * 1024**3)


def test_command_is_hardened_and_contains_no_host_mount_or_env(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    calls = []
    payloads = []

    def control(argv, **kwargs):
        calls.append((argv, kwargs))
        return _success(argv)

    def communicate(argv, payload, **kwargs):
        payloads.append((argv, payload, kwargs))
        return 0, b'{"accepted":true}', b""

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(python_sandbox, "_communicate_bounded", communicate)

    result = sandbox.run_json({"op": "validate", "source": "print('untrusted')"})

    assert result.response == {"accepted": True}
    assert result.container_name.startswith("vibe-py-worker-")
    assert calls[0][0][:4] == ["docker", "--context", "colima-vibe-execution", "info"]
    assert calls[1][0][-1] == "{{json .SecurityOptions}}"
    assert calls[2][0][3:5] == ["image", "inspect"]
    run_argv, payload, bounds = payloads[0]
    assert payload == b'{"op":"validate","source":"print(\'untrusted\')"}\n'
    assert bounds["timeout"] == sandbox.limits.timeout_seconds
    assert bounds["max_output_bytes"] == sandbox.limits.max_output_bytes
    assert "--network" in run_argv and run_argv[run_argv.index("--network") + 1] == "none"
    assert run_argv[run_argv.index("--user") + 1] == "65532:65532"
    assert "--read-only" in run_argv
    assert run_argv[run_argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in run_argv
    assert "seccomp=builtin" in run_argv
    assert "--pids-limit" in run_argv and "--memory" in run_argv and "--cpus" in run_argv
    assert run_argv.count("--tmpfs") == 2
    assert not any(arg in {"--mount", "-v", "--volume", "--env", "-e", "--privileged"} for arg in run_argv)
    assert not any(arg in {"--pid=host", "--ipc=host", "--network=host", "--device"} for arg in run_argv)
    assert "--rm" in run_argv
    assert run_argv[run_argv.index("--entrypoint") + 1] == "/usr/bin/timeout"
    image_index = run_argv.index(IMAGE)
    assert run_argv[image_index + 1 : image_index + 7] == [
        "--kill-after=2s", "--signal=TERM", "30s", "python", "-I", "/opt/test-worker.py",
    ]
    assert calls[-1][0][-3:] == ["--force", "--volumes", result.container_name]


def test_daemon_or_image_preflight_failure_fails_closed(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    calls = []

    def control(argv, **kwargs):
        calls.append(argv)
        if "image" in argv:
            raise SandboxUnavailable("image missing")
        return _success(argv)

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: pytest.fail("must not start a container before image preflight"),
    )

    with pytest.raises(SandboxUnavailable, match="image missing"):
        sandbox.run_json({"op": "validate"})
    assert len(calls) == 3


@pytest.mark.parametrize(
    "security_options",
    [
        b"[]",
        b'["name=seccomp,profile=unconfined"]',
        b'["name=seccomp,profile=custom"]',
        b'["name=seccomp"]',
        b"not-json",
        b"{}",
    ],
)
def test_missing_unconfined_or_unrecognized_seccomp_fails_before_worker(
    monkeypatch, security_options: bytes,
):
    sandbox = PythonSandbox(IMAGE)
    calls = []

    def control(argv, **kwargs):
        calls.append(argv)
        if "SecurityOptions" in argv[-1]:
            return subprocess.CompletedProcess([], 0, stdout=security_options, stderr=b"")
        return _success(argv)

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: pytest.fail("worker must not start with unverified seccomp"),
    )

    with pytest.raises(SandboxUnavailable, match="seccomp"):
        sandbox.run_json({"op": "validate"})
    assert len(calls) == 2
    assert "image" not in calls[-1]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (SandboxTimeout("wall clock"), SandboxTimeout),
        (SandboxOutputLimit("output cap"), SandboxOutputLimit),
    ],
)
def test_timeout_and_output_limit_always_attempt_container_cleanup(monkeypatch, failure, expected):
    sandbox = PythonSandbox(IMAGE)
    cleanup_calls = []

    def control(argv, **kwargs):
        if argv[3] == "rm":
            cleanup_calls.append(argv)
        return _success(argv)

    def communicate(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(python_sandbox, "_communicate_bounded", communicate)

    with pytest.raises(expected):
        sandbox.run_json({"op": "validate"})
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][4] == "--force"


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (17, b"", b"worker failed", "status 17"),
        (0, b"not-json", b"", "JSON response"),
    ],
)
def test_worker_errors_always_attempt_container_cleanup(monkeypatch, returncode, stdout, stderr, message):
    sandbox = PythonSandbox(IMAGE)
    cleanup_calls = []

    def control(argv, **kwargs):
        if argv[3] == "rm":
            cleanup_calls.append(argv)
        return _success(argv)

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: (returncode, stdout, stderr),
    )

    with pytest.raises(python_sandbox.SandboxExecutionError, match=message):
        sandbox.run_json({"op": "validate"})
    assert len(cleanup_calls) == 1


def test_normalized_strategy_failure_discards_worker_controlled_text_and_cleans_container(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    cleanup_calls = []

    def control(argv, **kwargs):
        if argv[3] == "rm":
            cleanup_calls.append(argv)
        return _success(argv)

    monkeypatch.setattr(sandbox, "_control", control)
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: (
            2,
            b'{"worker_error":"strategy_runtime_error","error_type":"NameError","source_line":4}',
            b"private strategy output SECRET-MARKER",
        ),
    )

    with pytest.raises(python_sandbox.SandboxStrategyFailure) as caught:
        sandbox.run_json({"op": "strategy_event"})
    assert caught.value.category == "strategy_runtime_error"
    assert caught.value.error_type == "NameError"
    assert caught.value.source_line == 4
    assert "SECRET-MARKER" not in str(caught.value)
    assert len(cleanup_calls) == 1


def test_malformed_strategy_failure_is_not_accepted_as_normalized(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    monkeypatch.setattr(sandbox, "_control", lambda argv, **_kwargs: _success(argv))
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: (
            2,
            b'{"worker_error":"strategy_runtime_error","error_type":"SecretType","source_line":999999999}',
            b"worker failed",
        ),
    )

    with pytest.raises(python_sandbox.SandboxExecutionError):
        sandbox.run_json({"op": "strategy_event"})


def test_container_exit_124_is_reported_as_internal_deadline_timeout(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    monkeypatch.setattr(sandbox, "_control", lambda argv, **_kwargs: _success(argv))
    monkeypatch.setattr(
        python_sandbox,
        "_communicate_bounded",
        lambda *_args, **_kwargs: (124, b"", b""),
    )

    with pytest.raises(SandboxTimeout, match="in-container"):
        sandbox.run_json({"op": "validate"})


def test_auto_removed_container_is_already_clean(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    calls = []

    def control(argv, **kwargs):
        calls.append(argv)
        raise SandboxUnavailable("worker container cleanup failed: Error: No such container: worker")

    monkeypatch.setattr(sandbox, "_control", control)
    sandbox._remove_container("worker")
    assert len(calls) == 1


def test_cleanup_does_not_hide_daemon_failure(monkeypatch):
    sandbox = PythonSandbox(IMAGE)
    monkeypatch.setattr(
        sandbox,
        "_control",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SandboxUnavailable("Cannot connect to Docker daemon")),
    )

    with pytest.raises(SandboxUnavailable, match="Cannot connect"):
        sandbox._remove_container("worker")


def test_request_is_bounded_before_preflight(monkeypatch):
    sandbox = PythonSandbox(IMAGE, limits=SandboxLimits(max_request_bytes=8))
    monkeypatch.setattr(sandbox, "preflight", lambda: pytest.fail("oversized request must be rejected first"))

    with pytest.raises(ValueError, match="byte limit"):
        sandbox.run_json({"payload": "too large"})


def test_bounded_pipe_helper_round_trips_json_without_shell(tmp_path):
    # Fake the Docker CLI transport only; no uploaded/user Python is evaluated.
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '{\"worker\":\"stub\"}'\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    previous_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = f"{tmp_path}:{previous_path}"
        returncode, stdout, stderr = python_sandbox._communicate_bounded(
            [str(fake_docker)],
            b'{"request":1}\n',
            timeout=2,
            max_output_bytes=128,
        )
    finally:
        os.environ["PATH"] = previous_path

    assert returncode == 0
    assert json.loads(stdout) == {"worker": "stub"}
    assert stderr == b""
