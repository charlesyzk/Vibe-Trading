"""Local exclusive owner lease for one broker account/environment runtime."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from src.execution.contracts import Environment


class ExecutionOwnerUnavailable(RuntimeError):
    """Another local process already owns this account/environment scope."""


class ExecutionOwnerLease:
    """Hold an advisory OS lock for one exact broker/account/environment.

    The lock is local to a shared filesystem and is not a distributed lease.
    Keep the instance alive for the full execution-service lifetime.
    """

    def __init__(
        self,
        *,
        lock_directory: str | Path,
        broker: str,
        account_ref: str,
        environment: Environment,
        owner_id: str,
    ) -> None:
        if not broker or not account_ref or not owner_id:
            raise ValueError("broker, account_ref, and owner_id are required")
        self.broker = broker
        self.account_ref = account_ref
        self.environment = Environment(environment)
        self.owner_id = owner_id
        root = Path(lock_directory).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        scope = json.dumps(
            [self.broker, self.account_ref, self.environment.value],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.path = root / ("execution-owner-" + hashlib.sha256(scope).hexdigest() + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "r+b")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            raise ExecutionOwnerUnavailable(
                "another local process owns this broker/account/environment"
            ) from exc
        metadata: dict[str, Any] = {
            "broker": self.broker,
            "account_ref": self.account_ref,
            "environment": self.environment.value,
            "owner_id": self.owner_id,
            "pid": os.getpid(),
        }
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._file.seek(0)
        self._file.truncate()
        self._file.write(encoded)
        self._file.flush()
        os.fsync(self._file.fileno())

    @property
    def held(self) -> bool:
        return not self._file.closed

    def assert_scope(self, *, broker: str, account_ref: str, environment: Environment) -> None:
        if not self.held:
            raise ExecutionOwnerUnavailable("execution owner lease is not held")
        if (
            broker != self.broker
            or account_ref != self.account_ref
            or Environment(environment) is not self.environment
        ):
            raise ExecutionOwnerUnavailable("intent scope differs from the held execution owner")

    def close(self) -> None:
        if not self._file.closed:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()

    def __enter__(self) -> "ExecutionOwnerLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
