"""Nonblocking local process lock on an already-open binary file."""

from __future__ import annotations

import errno
import os
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire(file: BinaryIO) -> None:
    if os.name == "nt":
        # Keep the locked byte away from lease metadata written at offset zero.
        file.seek(1 << 20)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError("local lock is already held") from exc
            raise
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(file: BinaryIO) -> None:
    if os.name == "nt":
        file.seek(1 << 20)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
