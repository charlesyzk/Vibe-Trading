"""Durable host-side IBKR order-id allocator.

The SDK runs in a separate environment, so the trusted host owns this gate
and observes ``nextValidId`` through the callback pump. A fresh value is only
a candidate: reconciliation must explicitly confirm it before dispatch can
allocate an id.
"""

from __future__ import annotations

import fcntl
import sqlite3
from contextlib import closing
from pathlib import Path


class OrderIdNotReady(RuntimeError):
    """No fresh, reconciled order id is available for allocation."""


class IbkrOrderIdAllocator:
    """Persist a monotone cursor and exclude concurrent local allocators."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = str(Path(database_path).expanduser().resolve())
        self._lock_file = open(self.path + ".order-id-owner.lock", "a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(
                "another local execution process owns this IBKR order-id store"
            ) from exc
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS ibkr_order_id_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        next_order_id INTEGER NOT NULL CHECK(next_order_id > 0),
                        ready INTEGER NOT NULL CHECK(ready IN (0,1)),
                        candidate INTEGER NOT NULL DEFAULT 0 CHECK(candidate IN (0,1))
                    )"""
                )
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(ibkr_order_id_state)")
                }
                if "candidate" not in columns:
                    db.execute(
                        "ALTER TABLE ibkr_order_id_state "
                        "ADD COLUMN candidate INTEGER NOT NULL DEFAULT 0"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO ibkr_order_id_state"
                    "(singleton,next_order_id,ready,candidate) VALUES(1,1,0,0)"
                )
                # Each runtime epoch needs a new broker cursor and reconciliation.
                db.execute(
                    "UPDATE ibkr_order_id_state SET ready=0,candidate=0 WHERE singleton=1"
                )
                db.commit()
        except Exception:
            self.close()
            raise

    def observe_next_valid_id(self, order_id: int) -> None:
        """Store a broker candidate without enabling allocation."""
        if type(order_id) is not int or order_id <= 0:
            raise ValueError("nextValidId must be a positive integer")
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_order_id FROM ibkr_order_id_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                db.rollback()
                raise OrderIdNotReady("durable order-id state is missing")
            if order_id < int(row[0]):
                db.execute(
                    "UPDATE ibkr_order_id_state SET ready=0,candidate=0 WHERE singleton=1"
                )
                db.commit()
                raise OrderIdNotReady(
                    "broker nextValidId is below the durable local cursor; reconcile before allocating"
                )
            db.execute(
                "UPDATE ibkr_order_id_state SET next_order_id=?,ready=0,candidate=1 "
                "WHERE singleton=1",
                (order_id,),
            )
            db.commit()

    def confirm_reconciled(self, *, expected_order_id: int | None = None) -> None:
        """Enable this epoch only after trusted broker/order reconciliation."""
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_order_id,ready,candidate FROM ibkr_order_id_state WHERE singleton=1"
            ).fetchone()
            if (
                row is None or row[1] or not row[2]
                or (expected_order_id is not None and row[0] != expected_order_id)
            ):
                db.rollback()
                raise OrderIdNotReady(
                    "the fresh nextValidId candidate changed or was not observed before reconciliation"
                )
            db.execute(
                "UPDATE ibkr_order_id_state SET ready=1,candidate=0 WHERE singleton=1"
            )
            db.commit()

    def candidate_for_reconciliation(self) -> int:
        """Read the unconfirmed broker cursor without opening allocation."""
        with closing(sqlite3.connect(self.path, timeout=5.0)) as db:
            row = db.execute(
                "SELECT next_order_id,ready,candidate FROM ibkr_order_id_state WHERE singleton=1"
            ).fetchone()
        if row is None or row[1] or not row[2]:
            raise OrderIdNotReady("an unconfirmed nextValidId candidate is required")
        return int(row[0])

    def require_fresh_candidate(self) -> None:
        """Close allocation and forget the candidate before a new reqIds call."""
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE ibkr_order_id_state SET ready=0,candidate=0 WHERE singleton=1"
            )
            db.commit()

    @property
    def allocation_ready(self) -> bool:
        with closing(sqlite3.connect(self.path, timeout=5.0)) as db:
            row = db.execute(
                "SELECT ready FROM ibkr_order_id_state WHERE singleton=1"
            ).fetchone()
        return row is not None and bool(row[0])

    def allocate(self) -> int:
        """Commit the next id before returning it to the dispatch path."""
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_order_id,ready FROM ibkr_order_id_state WHERE singleton=1"
            ).fetchone()
            if row is None or not row[1]:
                db.rollback()
                raise OrderIdNotReady("nextValidId has not been received and reconciled")
            order_id = int(row[0])
            db.execute(
                "UPDATE ibkr_order_id_state SET next_order_id=? WHERE singleton=1",
                (order_id + 1,),
            )
            db.commit()
            return order_id

    def close(self) -> None:
        if not self._lock_file.closed:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def __enter__(self) -> "IbkrOrderIdAllocator":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
