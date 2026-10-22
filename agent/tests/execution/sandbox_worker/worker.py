"""Small stdlib-only worker used by the opt-in Docker isolation test."""

from __future__ import annotations

import gc
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


MAX_REQUEST_BYTES = 1024 * 1024


def _connect_fails(host: str, port: int) -> bool:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(0.5)
    try:
        connection.connect((host, port))
    except OSError:
        return True
    else:
        return False
    finally:
        connection.close()


def _root_filesystem_write_fails() -> bool:
    path = "/vibe-sandbox-rootfs-write-probe"
    try:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("unexpected writable root filesystem")
    except OSError:
        return True
    else:
        os.unlink(path)
        return False


def _cgroup_value(name: str) -> str | None:
    try:
        return Path(f"/sys/fs/cgroup/{name}").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _probe_pids_limit() -> dict[str, int]:
    """Try to exceed the cgroup task cap, then reap every task we started."""
    children: list[subprocess.Popen[bytes]] = []
    failures = 0
    for _ in range(12):
        try:
            children.append(subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.25)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ))
        except OSError:
            failures += 1
    for child in children:
        child.wait(timeout=2)
    return {"spawned": len(children), "denied": failures}


def _probe_memory_limit() -> dict[str, int | bool | str | None]:
    """Touch memory until the container cgroup refuses more pages."""
    blocks: list[bytearray] = []
    denied = False
    try:
        for _ in range(128):
            blocks.append(bytearray(1024 * 1024))
    except MemoryError:
        denied = True
    allocated_bytes = len(blocks) * 1024 * 1024
    blocks.clear()
    gc.collect()
    return {
        "allocated_bytes": allocated_bytes,
        "denied": denied,
        "cgroup_memory_max": _cgroup_value("memory.max"),
    }


def _probe_cpu_limit(seconds: float = 2.0) -> dict[str, int | float]:
    raw = _cgroup_value("cpu.stat")
    if raw is None:
        raise RuntimeError("cgroup cpu.stat is unavailable")
    fields = dict(line.split() for line in raw.splitlines() if len(line.split()) == 2)
    before = int(fields["usage_usec"])
    start = time.monotonic()
    deadline = start + seconds
    iterations = 0
    while time.monotonic() < deadline:
        iterations += 1
    wall_seconds = time.monotonic() - start
    updated = _cgroup_value("cpu.stat")
    if updated is None:
        raise RuntimeError("cgroup cpu.stat disappeared during probe")
    updated_fields = dict(line.split() for line in updated.splitlines() if len(line.split()) == 2)
    return {
        "wall_seconds": wall_seconds,
        "usage_usec": int(updated_fields["usage_usec"]) - before,
        "iterations": iterations,
    }


def main() -> int:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        print(json.dumps({"error": "request too large or unterminated"}))
        return 2
    request = json.loads(raw)
    if request.get("op") != "sandbox_probe":
        print(json.dumps({"error": "unsupported probe operation"}))
        return 2

    sentinel = request["host_file_sentinel"]
    try:
        with open(sentinel, "rb") as stream:
            host_file_read = stream.read(64) == request["host_file_contents"].encode("utf-8")
    except OSError:
        host_file_read = False

    response = {
        "op": "sandbox_probe",
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "rootfs_write_denied": _root_filesystem_write_fails(),
        "host_file_read": host_file_read,
        "host_loopback_connect_denied": _connect_fails("127.0.0.1", request["host_loopback_port"]),
        "external_connect_denied": _connect_fails("1.1.1.1", 53),
        "host_env_marker_present": os.environ.get(request["host_env_marker_name"]) == request["host_env_marker_value"],
        "cgroup_memory_max": _cgroup_value("memory.max"),
        "cgroup_cpu_max": _cgroup_value("cpu.max"),
        "cgroup_pids_max": _cgroup_value("pids.max"),
    }
    if request.get("probe_pids_limit") is True:
        response["pids_probe"] = _probe_pids_limit()
    if request.get("probe_memory_limit") is True:
        response["memory_probe"] = _probe_memory_limit()
    if request.get("probe_cpu_limit") is True:
        response["cpu_probe"] = _probe_cpu_limit()
    hold_seconds = min(max(float(request.get("hold_seconds", 0)), 0), 5)
    if hold_seconds:
        time.sleep(hold_seconds)
    print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
