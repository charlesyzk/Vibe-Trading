"""Offline contract tests for the pinned official IBKR API installer."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install_ibapi_from_official_source.py"
SPEC = importlib.util.spec_from_file_location("install_ibapi_from_official_source", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def _source_tree(tmp_path: Path, *, version: str = "10.50.2") -> Path:
    source = tmp_path / "source" / "pythonclient"
    package = source / "ibapi"
    package.mkdir(parents=True)
    (source / "setup.py").write_text("from setuptools import setup\nsetup(name='ibapi')\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        f"def get_version_string():\n    return {version!r}\n", encoding="utf-8"
    )
    (package / "client.py").write_text("class EClient: pass\n", encoding="utf-8")
    return source


def _lock_for(source: Path) -> dict[str, object]:
    digest, count = installer.source_tree_sha256(source)
    return {
        "source_tree_sha256": digest,
        "source_file_count": count,
        "python_distribution_version": "10.50.2",
        "locked_build_dependencies": {"protobuf": "5.29.5", "setuptools": "80.9.0", "wheel": "0.45.1"},
    }


def test_verified_official_source_tree_matches_exact_file_hash_and_version(tmp_path):
    source = _source_tree(tmp_path)
    lock = _lock_for(source)

    assert installer.verify_source(source, lock) == lock["source_tree_sha256"]

    (source / "ibapi" / "client.py").write_text("class EClient: pass  # changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the pinned release"):
        installer.verify_source(source, lock)


def test_source_tree_rejects_symlinks_even_when_the_target_is_inside_the_tree(tmp_path):
    source = _source_tree(tmp_path)
    target = source / "ibapi" / "client.py"
    target.unlink()
    target.symlink_to(source / "setup.py")

    with pytest.raises(ValueError, match="refusing symlink"):
        installer.source_tree_sha256(source)


def test_source_tree_version_mismatch_fails_before_install(tmp_path):
    source = _source_tree(tmp_path, version="10.50.1")
    lock = _lock_for(source)

    with pytest.raises(ValueError, match="expected ibapi 10.50.2, got 10.50.1"):
        installer.verify_source(source, lock)


def test_install_requires_exact_locked_build_dependencies_before_calling_uv(monkeypatch, tmp_path):
    source = _source_tree(tmp_path)
    target_python = tmp_path / "venv" / "bin" / "python"
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "import ibapi; print(ibapi.get_version_string())" in argv[2]:
            return subprocess.CompletedProcess(argv, 0, stdout="10.50.2\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"protobuf": "5.29.5", "setuptools": "80.9.0", "wheel": "0.45.0"}),
            stderr="",
        )

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer.shutil, "which", lambda _: pytest.fail("uv must not run with mismatched dependencies"))

    with pytest.raises(RuntimeError, match="wheel==0.45.1"):
        installer.install(source, target_python, _lock_for(source))

    assert len(calls) == 2
    assert json.loads(calls[1][3]) == _lock_for(source)["locked_build_dependencies"]


def test_install_uses_uv_no_build_isolation_no_dependencies_and_exact_venv_python(monkeypatch, tmp_path):
    source = _source_tree(tmp_path)
    target_python = tmp_path / "venv" / "bin" / "python"
    dependency_output = json.dumps({"protobuf": "5.29.5", "setuptools": "80.9.0", "wheel": "0.45.1"})
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "import ibapi; print(ibapi.get_version_string())" in argv[2]:
            return subprocess.CompletedProcess(argv, 0, stdout="10.50.2\n", stderr="")
        if "expected = json.loads(sys.argv[1])" in argv[2]:
            return subprocess.CompletedProcess(argv, 0, stdout=dependency_output, stderr="")
        if argv[:3] == ["uv-test", "pip", "install"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="10.50.2\n", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "uv-test" if name == "uv" else None)

    installer.install(source, target_python, _lock_for(source))

    assert calls[2][0] == [
        "uv-test", "pip", "install", "--python", str(target_python), "--no-build-isolation", "--no-deps", str(source)
    ]
    assert calls[2][1]["check"] is True
    assert calls[3][0] == [
        str(target_python), "-c", "import importlib.metadata as m; print(m.version('ibapi'))"
    ]


def test_install_rejects_missing_uv_after_dependency_check(monkeypatch, tmp_path):
    source = _source_tree(tmp_path)
    target_python = tmp_path / "venv" / "bin" / "python"
    output = json.dumps({"protobuf": "5.29.5", "setuptools": "80.9.0", "wheel": "0.45.1"})
    def fake_run(argv, **kwargs):
        if "import ibapi; print(ibapi.get_version_string())" in argv[2]:
            return subprocess.CompletedProcess(argv, 0, stdout="10.50.2\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)

    with pytest.raises(RuntimeError, match="uv is required"):
        installer.install(source, target_python, _lock_for(source))


def test_install_reverifies_source_before_inspecting_target_or_calling_uv(monkeypatch, tmp_path):
    source = _source_tree(tmp_path)
    lock = _lock_for(source)
    (source / "ibapi" / "client.py").write_text("class EClient: changed\n", encoding="utf-8")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unverified source must fail before subprocess execution"),
    )
    monkeypatch.setattr(installer.shutil, "which", lambda _: pytest.fail("uv must not be inspected"))

    with pytest.raises(ValueError, match="does not match the pinned release"):
        installer.install(source, tmp_path / "venv" / "bin" / "python", lock)
