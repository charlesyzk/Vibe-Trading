"""Resolve the trusted isolated Python worker selected by the local host."""

from __future__ import annotations

import os

from src.execution.wasi_python_sandbox import WASI_WORKER_ID, WasiPythonSandbox


def configured_worker_identity() -> str:
    if os.environ.get("VIBE_WASI_WORKER_ROOT", "").strip():
        return WASI_WORKER_ID
    return os.environ.get("VIBE_PYTHON_WORKER_IMAGE", "").strip()


def create_worker(identity: str):
    if identity == WASI_WORKER_ID:
        root = os.environ.get("VIBE_WASI_WORKER_ROOT", "").strip()
        if not root:
            raise ValueError("the pinned WASI worker runtime is not configured")
        return WasiPythonSandbox(root)
    from src.execution.python_sandbox import PythonSandbox
    return PythonSandbox(identity)
