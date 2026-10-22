#!/usr/bin/env python3
"""Offline macOS Seatbelt and resource-limit probe. No account or trading access."""
from __future__ import annotations

import resource
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def run_sandbox(profile: str, code: str, *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    print(f"platform={sys.platform} python={sys.version.split()[0]}")
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    print(f"sandbox_exec_exists={sandbox_exec.exists()}")
    try:
        import pwd
        pwd.getpwnam("vibe-sandbox")
        sandbox_user = True
    except (ImportError, KeyError, OSError):
        sandbox_user = False
    print(f"vibe_sandbox_account_exists={sandbox_user}")

    # Compare a descendant process reaching a loopback listener with and
    # without Seatbelt. The listener binds only to an ephemeral localhost port.
    for label, sandboxed in (("baseline", False), ("seatbelt", True)):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        host, port = listener.getsockname()
        accepted: list[bool] = []

        def accept_once(sock: socket.socket = listener) -> None:
            try:
                conn, _ = sock.accept()
                accepted.append(True)
                conn.close()
            except (TimeoutError, socket.timeout, OSError):
                accepted.append(False)

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        child_code = (
            "import socket,sys; s=socket.socket(); "
            f"s.connect(({host!r}, {port})); print('CONNECTED')"
        )
        code = (
            "import subprocess,sys; "
            f"r=subprocess.run([sys.executable, '-c', {child_code!r}], "
            "text=True, capture_output=True); "
            "print('descendant_rc=' + str(r.returncode)); "
            "print(r.stdout.strip()); print(r.stderr.strip())"
        )
        if sandboxed:
            result = run_sandbox("(version 1) (allow default) (deny network*)", code)
        else:
            result = subprocess.run([sys.executable, "-c", code], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=5, check=False)
        thread.join(timeout=4)
        listener.close()
        print(f"network_{label}_returncode={result.returncode}")
        print(f"network_{label}_stdout={result.stdout.strip()!r}")
        print(f"network_{label}_stderr={result.stderr.strip()!r}")
        print(f"network_{label}_listener_accepted={accepted[0] if accepted else 'unknown'}")

    # Deny reads of one temporary sentinel file while allowing ordinary Python
    # runtime reads. The sentinel contains no real user data.
    with tempfile.TemporaryDirectory(prefix="vt-sandbox-probe-") as tmp:
        sentinel = (Path(tmp) / "synthetic-secret.txt").resolve()
        sentinel.write_text("synthetic sentinel only", encoding="utf-8")
        print(f"file_baseline_read={sentinel.read_text(encoding='utf-8')!r}")
        profile = f'(version 1) (allow default) (deny file-read* (literal "{sentinel}"))'
        code = """import sys
p=sys.argv[1]
try:
    print('READ:' + open(p).read())
    sys.exit(0)
except OSError as e:
    print(type(e).__name__ + ':' + str(e))
    sys.exit(4)
"""
        file_result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", code, str(sentinel)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        print(f"file_returncode={file_result.returncode}")
        print(f"file_stdout={file_result.stdout.strip()!r}")
        print(f"file_stderr={file_result.stderr.strip()!r}")

    # Each limit is applied only inside a disposable probe child.
    resource_cases = {
        "cpu": (
            "resource.RLIMIT_CPU", "(1, 2)",
            "exec('while True: pass')", 5,
        ),
        "address_space": (
            "resource.RLIMIT_AS", "(4096 * 1024 * 1024, 4096 * 1024 * 1024)",
            "print('SET_LIMIT_SUCCEEDED')", 5,
        ),
        "data_segment": (
            "resource.RLIMIT_DATA", "(128 * 1024 * 1024, resource.RLIM_INFINITY)",
            "bytearray(256 * 1024 * 1024)", 5,
        ),
        "file_size": (
            "resource.RLIMIT_FSIZE", "(1024, 1024)",
            "open(sys.argv[1], 'wb').write(b'x' * 4096)", 5,
        ),
        "open_files": (
            "resource.RLIMIT_NOFILE", "(16, 16)",
            "fds=[open('/dev/null') for _ in range(64)]", 5,
        ),
    }
    for name, (limit_name, value, action, timeout) in resource_cases.items():
        with tempfile.TemporaryDirectory(prefix="vt-rlimit-probe-") as tmp:
            target = str(Path(tmp) / "bounded-output.bin")
            code = (
                "import resource,sys; "
                f"resource.setrlimit({limit_name}, {value}); "
                f"{action}; print('ACTION_COMPLETED')"
            )
            args = [sys.executable, "-c", code]
            if name == "file_size":
                args.append(target)
            try:
                result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=timeout, check=False)
                size = Path(target).stat().st_size if Path(target).exists() else None
                print(f"rlimit_{name}_returncode={result.returncode}")
                print(f"rlimit_{name}_stdout={result.stdout.strip()!r}")
                print(f"rlimit_{name}_stderr={result.stderr.strip()!r}")
                if name == "file_size":
                    print(f"rlimit_{name}_output_size={size}")
            except subprocess.TimeoutExpired:
                print(f"rlimit_{name}_result=parent-timeout")

    print("resource_current=" + repr({
        name: resource.getrlimit(getattr(resource, name))
        for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_RSS",
                     "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_NPROC")
        if hasattr(resource, name)
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
