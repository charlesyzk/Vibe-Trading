"""Real Docker isolation verification; deliberately fails when prerequisites are absent."""

from __future__ import annotations

import json
import os
import pwd
import re
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.execution.python_sandbox import PythonSandbox, SandboxExecutionError, SandboxLimits


CONTEXT_DEFAULT = "colima-vibe-execution"
BASE_IMAGE = "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
WORKER_DIR = Path(__file__).parent / "sandbox_worker"


def _docker(context: str, *args: str, env: dict[str, str], timeout: float = 20) -> subprocess.CompletedProcess[bytes]:
    argv = ["docker", "--context", context, *args]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"required Docker integration command could not run: {argv!r}: {exc}")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:2000]
        pytest.fail(f"required Docker integration command failed ({result.returncode}): {argv!r}: {detail}")
    return result


@pytest.mark.integration
def test_python_worker_is_confined_by_real_vibe_execution_docker_context(tmp_path, monkeypatch):
    """Build offline from a digest-pinned cached base, then test actual container policy."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    # agent/tests/conftest.py intentionally redirects HOME. Repoint only the
    # Docker CLI to the real user's Docker config so this explicit integration
    # probe can use the dedicated Colima context metadata.
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    configured_docker = os.environ.get("DOCKER_CONFIG")
    docker_config = Path(configured_docker) if configured_docker else actual_home / ".docker"
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: Docker config directory not found: {docker_config}")
    docker_env = dict(os.environ)
    docker_env["DOCKER_CONFIG"] = str(docker_config)
    docker_env["DOCKER_CONTEXT"] = context
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)

    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)
    base = _docker(context, "image", "inspect", "--format", "{{.Id}}", BASE_IMAGE, env=docker_env)
    assert base.stdout.decode().strip().startswith("sha256:"), "pinned offline base image is not cached locally"

    image_tag = f"vibe-python-sandbox-probe:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    host_listener: socket.socket | None = None
    try:
        _docker(
            context,
            "build",
            "--network=none",
            "--pull=false",
            "--file",
            str(WORKER_DIR / "Dockerfile"),
            "--tag",
            image_tag,
            str(WORKER_DIR),
            env=docker_env,
            timeout=120,
        )
        inspect_image = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag, env=docker_env)
        worker_image_id = inspect_image.stdout.decode().strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", worker_image_id), f"unexpected immutable image ID: {worker_image_id!r}"

        sentinel = tmp_path / "host-only-sentinel.txt"
        sentinel_contents = f"host-secret-{uuid.uuid4().hex}"
        sentinel.write_text(sentinel_contents, encoding="utf-8")
        marker_name = "VIBE_SANDBOX_HOST_SENTINEL"
        marker_value = f"host-env-{uuid.uuid4().hex}"
        monkeypatch.setenv(marker_name, marker_value)
        docker_env[marker_name] = marker_value

        host_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host_listener.bind(("127.0.0.1", 0))
        host_listener.listen(1)
        host_listener.settimeout(0.2)
        host_port = host_listener.getsockname()[1]
        request = {
            "op": "sandbox_probe",
            "host_file_sentinel": str(sentinel),
            "host_file_contents": sentinel_contents,
            "host_loopback_port": host_port,
            "host_env_marker_name": marker_name,
            "host_env_marker_value": marker_value,
            "hold_seconds": 3,
            "probe_pids_limit": True,
            "probe_cpu_limit": True,
        }

        sandbox = PythonSandbox(
            worker_image_id,
            context=context,
            limits=SandboxLimits(
                memory_bytes=128 * 1024 * 1024,
                cpu_count=0.5,
                pids_limit=4,
                timeout_seconds=12,
                max_request_bytes=16 * 1024,
            ),
        )
        existing = _docker(context, "ps", "--format", "{{.Names}}", env=docker_env)
        existing_names = set(existing.stdout.decode().splitlines())
        inspected_container: dict[str, object] | None = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sandbox.run_json, request)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not future.done():
                names = _docker(
                    context,
                    "ps",
                    "--filter",
                    "name=vibe-py-worker-",
                    "--format",
                    "{{.Names}}",
                    env=docker_env,
                ).stdout.decode().splitlines()
                candidates = [name for name in names if name not in existing_names]
                if candidates:
                    raw_inspect = _docker(context, "inspect", "--format", "{{json .}}", candidates[0], env=docker_env)
                    inspected_container = json.loads(raw_inspect.stdout)
                    break
                time.sleep(0.05)
            assert inspected_container is not None, "worker container never became inspectable in the dedicated Docker context"
            result = future.result(timeout=15)
        try:
            host_listener.accept()
        except TimeoutError:
            pass
        else:
            pytest.fail("worker reached a host loopback listener")
        host_listener.close()
        host_listener = None

        removed = subprocess.run(
            ["docker", "--context", context, "inspect", result.container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=docker_env,
            timeout=10,
            check=False,
        )
        assert removed.returncode != 0, f"worker container survived cleanup: {result.container_name}"

        response = result.response
        assert response["uid"] > 0 and response["euid"] > 0, response
        assert response["rootfs_write_denied"] is True, response
        assert response["host_file_read"] is False, response
        assert response["host_loopback_connect_denied"] is True, response
        assert response["external_connect_denied"] is True, response
        assert response["host_env_marker_present"] is False, response
        assert response["cgroup_memory_max"] == str(128 * 1024 * 1024), response
        assert response["cgroup_cpu_max"].split()[0] == "50000", response
        assert response["cgroup_pids_max"] == "4", response
        assert response["pids_probe"]["spawned"] < 12, response
        assert response["pids_probe"]["denied"] > 0, response
        assert response["cpu_probe"]["wall_seconds"] >= 1.9, response
        assert response["cpu_probe"]["usage_usec"] <= 1_750_000, response
        assert response["cpu_probe"]["iterations"] > 0, response

        config = inspected_container["Config"]
        host_config = inspected_container["HostConfig"]
        mounts = inspected_container["Mounts"]
        assert config["User"] == "65532:65532", config
        assert marker_name not in " ".join(config["Env"] or []), config["Env"]
        assert host_config["ReadonlyRootfs"] is True, host_config
        assert host_config["NetworkMode"] == "none", host_config
        assert host_config["CapDrop"] == ["ALL"], host_config
        assert host_config["SecurityOpt"] == [
            "no-new-privileges:true",
            "seccomp=builtin",
        ], host_config
        assert host_config["Memory"] == 128 * 1024 * 1024, host_config
        assert host_config["MemorySwap"] == 128 * 1024 * 1024, host_config
        assert host_config["NanoCpus"] == 500_000_000, host_config
        assert host_config["PidsLimit"] == 4, host_config
        assert not host_config.get("Binds"), host_config
        assert not mounts, mounts
        assert host_config["Tmpfs"] == {
            "/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "/work": "rw,noexec,nosuid,nodev,size=67108864,mode=1777",
        }, host_config["Tmpfs"]

        # A separate low-memory container makes the limit consequential. A
        # caught MemoryError is acceptable; a cgroup OOM kill must exit 137.
        memory_sandbox = PythonSandbox(
            worker_image_id,
            context=context,
            limits=SandboxLimits(memory_bytes=32 * 1024 * 1024, timeout_seconds=8, max_request_bytes=16 * 1024),
        )
        removed_names: list[str] = []
        remove_container = memory_sandbox._remove_container

        def capture_removal(name: str) -> None:
            removed_names.append(name)
            remove_container(name)

        monkeypatch.setattr(memory_sandbox, "_remove_container", capture_removal)
        memory_request = {
            "op": "sandbox_probe",
            "host_file_sentinel": str(sentinel),
            "host_file_contents": sentinel_contents,
            "host_loopback_port": host_port,
            "host_env_marker_name": marker_name,
            "host_env_marker_value": marker_value,
            "probe_memory_limit": True,
        }
        try:
            memory_result = memory_sandbox.run_json(memory_request)
        except SandboxExecutionError as exc:
            assert "status 137" in str(exc), str(exc)
        else:
            assert memory_result.response["memory_probe"]["cgroup_memory_max"] == str(32 * 1024 * 1024)
            assert memory_result.response["memory_probe"]["denied"] is True
            assert memory_result.response["memory_probe"]["allocated_bytes"] < 128 * 1024 * 1024
        assert len(removed_names) == 1
        removed_memory_container = subprocess.run(
            ["docker", "--context", context, "inspect", removed_names[0]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=docker_env,
            timeout=10,
            check=False,
        )
        assert removed_memory_container.returncode != 0, removed_memory_container.stdout.decode(errors="replace")
    finally:
        if host_listener is not None:
            host_listener.close()
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)
