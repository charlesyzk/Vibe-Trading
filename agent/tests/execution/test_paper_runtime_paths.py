from pathlib import Path

import pytest

from src.execution.paper_integration_test_composition import _ibkr_runtime_paths


@pytest.mark.parametrize("windows,subdir,executable", [
    (True, "Scripts", "python.exe"), (False, "bin", "python"),
])
def test_source_checkout_runtime_path(tmp_path, windows, subdir, executable):
    backend = tmp_path / "backend" / "python"
    root = tmp_path / "checkout"
    python, cwd = _ibkr_runtime_paths(backend_python=backend, repository_root=root, windows=windows)
    assert cwd == root / "ibkr-runtime"
    assert python == cwd / ".venv" / subdir / executable


@pytest.mark.parametrize("windows,executable", [(True, "python.exe"), (False, "python")])
def test_packaged_runtime_takes_precedence_and_never_falls_back_when_incomplete(tmp_path, windows, executable):
    backend = tmp_path / "backend" / "python.exe"
    bundled = backend.parent / "ibkr-runtime"
    bundled.mkdir(parents=True)
    python, cwd = _ibkr_runtime_paths(backend_python=backend, repository_root=tmp_path / "checkout", windows=windows)
    assert cwd == bundled
    assert python == bundled / executable
    assert not python.exists()
