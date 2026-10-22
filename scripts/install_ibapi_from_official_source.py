#!/usr/bin/env python3
"""Verify and install the pinned official IBKR TWS Python API source tree.

First obtain the matching release from the official IBKR API download page,
accept its license there, and extract ``source/pythonclient``. The tree lock
covers setup.py and every file under ibapi/. It detects a changed source tree
before executing the upstream setup metadata or asking uv to build/install it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "docs" / "backtrader-system" / "ibapi-source.lock.json"


def load_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def source_tree_sha256(source_dir: Path) -> tuple[str, int]:
    """Hash stable relative paths and exact bytes, excluding build by-products."""
    root = source_dir.resolve(strict=True)
    package = root / "ibapi"
    setup = root / "setup.py"
    if not package.is_dir() or package.is_symlink() or not setup.is_file() or setup.is_symlink():
        raise ValueError("source_dir must contain a regular setup.py and ibapi/ source directory")
    files = [setup, *sorted(package.rglob("*"))]
    digest = hashlib.sha256()
    count = 0
    for path in files:
        if path.is_symlink():
            raise ValueError(f"refusing symlink in API source tree: {path.relative_to(root)}")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        count += 1
    return digest.hexdigest(), count


def verify_source(source_dir: Path, lock: dict[str, Any]) -> str:
    source_hash, file_count = source_tree_sha256(source_dir)
    if source_hash != lock["source_tree_sha256"] or file_count != lock["source_file_count"]:
        raise ValueError(
            "official IBKR source tree does not match the pinned release: "
            f"sha256={source_hash}, files={file_count}"
        )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_dir.resolve())
    version = subprocess.run(
        [sys.executable, "-c", "import ibapi; print(ibapi.get_version_string())"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        text=True,
        timeout=10,
    )
    if version.returncode != 0:
        raise ValueError(f"could not read official ibapi source version: {version.stderr.strip()}")
    actual_version = version.stdout.strip()
    if actual_version != lock["python_distribution_version"]:
        raise ValueError(f"expected ibapi {lock['python_distribution_version']}, got {actual_version}")
    return source_hash


def install(source_dir: Path, python: Path, lock: dict[str, Any]) -> None:
    # Keep this guard inside the reusable installer function as well as the
    # CLI flow. A direct caller must not be able to reach upstream setup.py
    # with a source tree that was never verified against the lock.
    verify_source(source_dir, lock)
    dependency_check = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m, json, sys\n"
            "expected = json.loads(sys.argv[1])\n"
            "actual = {}\n"
            "for name in expected:\n"
            "    try: actual[name] = m.version(name)\n"
            "    except m.PackageNotFoundError: actual[name] = 'missing'\n"
            "print(json.dumps(actual, sort_keys=True))\n",
            json.dumps(lock["locked_build_dependencies"]),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        timeout=10,
    )
    if dependency_check.returncode != 0:
        raise RuntimeError(f"could not inspect target interpreter packages: {dependency_check.stderr.strip()}")
    actual_dependencies = json.loads(dependency_check.stdout)
    for package, expected in lock["locked_build_dependencies"].items():
        actual = actual_dependencies.get(package, "missing")
        if actual != expected:
            raise RuntimeError(
                f"target interpreter needs {package}=={expected} (found {actual}); "
                "sync the isolated ibkr-runtime project into this Python first"
            )
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the verified local source tree")
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--no-build-isolation", "--no-deps", str(source_dir)],
        check=True,
    )
    installed_version = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('ibapi'))"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        timeout=10,
    )
    if installed_version.returncode != 0 or installed_version.stdout.strip() != lock["python_distribution_version"]:
        raise RuntimeError("installed ibapi version did not match the source lock")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="official extracted source/pythonclient directory")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="target virtualenv Python")
    parser.add_argument("--verify-only", action="store_true", help="verify tree and version without installing")
    args = parser.parse_args()
    lock = load_lock()
    source_hash = verify_source(args.source_dir, lock)
    if args.verify_only:
        print(f"verified ibapi {lock['python_distribution_version']} source sha256={source_hash}")
        return 0
    # Keep the virtualenv path itself; Path.resolve() follows bin/python to the
    # base interpreter and discards the virtualenv's site-packages.
    install(args.source_dir, args.python.absolute(), lock)
    print(f"installed official ibapi {lock['python_distribution_version']} from verified source sha256={source_hash}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ibapi source install failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
