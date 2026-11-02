from __future__ import annotations

import errno
from types import SimpleNamespace

import pytest

from src.execution import local_file_lock


def test_windows_lock_uses_separate_byte_and_releases_it(tmp_path, monkeypatch):
    calls = []
    api = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)
    monkeypatch.setattr(local_file_lock, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(local_file_lock, "msvcrt", api, raising=False)
    with (tmp_path / "lease").open("w+b") as file:
        api.locking = lambda fd, mode, size: calls.append((fd, mode, size, file.tell()))
        local_file_lock.acquire(file)
        file.seek(0)
        file.write(b"owner metadata")
        file.flush()
        local_file_lock.release(file)
    assert [(mode, size, offset) for _, mode, size, offset in calls] == [
        (1, 1, 1 << 20), (2, 1, 1 << 20),
    ]


def test_windows_contention_is_distinct_from_other_io_errors(tmp_path, monkeypatch):
    api = SimpleNamespace(LK_NBLCK=1)
    monkeypatch.setattr(local_file_lock, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(local_file_lock, "msvcrt", api, raising=False)
    with (tmp_path / "lease").open("w+b") as file:
        def fail(code):
            def locking(*_args):
                raise OSError(code, "fixture")
            return locking

        api.locking = fail(errno.EACCES)
        with pytest.raises(BlockingIOError):
            local_file_lock.acquire(file)
        api.locking = fail(errno.EIO)
        with pytest.raises(OSError) as error:
            local_file_lock.acquire(file)
        assert error.value.errno == errno.EIO
