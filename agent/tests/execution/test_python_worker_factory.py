import pytest

from src.execution import python_worker_factory as factory
from src.execution.python_sandbox import PythonSandbox


def test_worker_factory_binds_wasi_identity_and_never_treats_it_as_docker(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBE_WASI_WORKER_ROOT", raising=False)
    monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", "sha256:" + "a" * 64)
    assert factory.configured_worker_identity() == "sha256:" + "a" * 64
    assert isinstance(factory.create_worker(factory.configured_worker_identity()), PythonSandbox)
    with pytest.raises(ValueError, match="WASI worker runtime"):
        factory.create_worker(factory.WASI_WORKER_ID)
    monkeypatch.setenv("VIBE_WASI_WORKER_ROOT", str(tmp_path))
    monkeypatch.setattr(factory, "WasiPythonSandbox", lambda root: ("wasi", root))
    assert factory.configured_worker_identity() == factory.WASI_WORKER_ID
    assert factory.create_worker(factory.WASI_WORKER_ID) == ("wasi", str(tmp_path))
