"""Durable, non-dispatchable event history for offline Backtrader runs.

This journal deliberately has no account, deployment, environment, risk-grant,
order-ledger, or dispatch API. Recovery rebuilds a fresh Cerebro/BackBroker
from the first bar and reuses committed worker results for that exact input.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from src.execution.contracts import Bar, canonical_json, is_sha256
from src.execution.event_journal import WorkerEventResult


class BacktestJournalError(RuntimeError):
    """The durable offline run cannot be safely resumed."""


class BacktestRunMismatch(BacktestJournalError):
    pass


class SQLiteBacktestEventJournal:
    """SQLite event store scoped to one offline Backtrader run."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = str(path)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS backtest_run (
                    run_id TEXT PRIMARY KEY,
                    strategy_version_hash TEXT NOT NULL,
                    strategy_spec_hash TEXT NOT NULL,
                    run_config_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('RUNNING', 'COMPLETE', 'PAUSED')),
                    next_seq INTEGER NOT NULL CHECK(next_seq > 0),
                    checkpoint_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS backtest_event (
                    run_id TEXT NOT NULL REFERENCES backtest_run(run_id),
                    seq INTEGER NOT NULL CHECK(seq > 0),
                    event_id TEXT NOT NULL,
                    bar_json TEXT NOT NULL,
                    bar_sha256 TEXT NOT NULL,
                    result_json TEXT,
                    result_sha256 TEXT,
                    released_at TEXT,
                    committed_at TEXT,
                    PRIMARY KEY(run_id, seq),
                    UNIQUE(run_id, event_id)
                );
                """
            )

    def bind_run(
        self,
        *,
        run_id: str,
        strategy_version_hash: str,
        strategy_spec_hash: str,
        run_config_hash: str,
    ) -> None:
        for name, value in (
            ("strategy_version_hash", strategy_version_hash),
            ("strategy_spec_hash", strategy_spec_hash),
            ("run_config_hash", run_config_hash),
        ):
            if not is_sha256(value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM backtest_run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO backtest_run
                       (run_id, strategy_version_hash, strategy_spec_hash, run_config_hash,
                        state, next_seq, checkpoint_json)
                       VALUES (?, ?, ?, ?, 'RUNNING', 1, '{}')""",
                    (run_id, strategy_version_hash, strategy_spec_hash, run_config_hash),
                )
                connection.commit()
                return
            expected = (strategy_version_hash, strategy_spec_hash, run_config_hash)
            actual = (row["strategy_version_hash"], row["strategy_spec_hash"], row["run_config_hash"])
            if actual != expected:
                connection.execute("UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
                connection.commit()
                raise BacktestRunMismatch("strategy version, spec, data or run configuration changed; run paused")
            connection.commit()

    def get_or_process_event(
        self,
        *,
        run_id: str,
        seq: int,
        event_id: str,
        bar: Bar,
        source: str,
        sandbox: Any,
    ) -> tuple[WorkerEventResult, bool]:
        """Return a committed result or execute and atomically commit one bar.

        The boolean is true when the result was already durable and reused.
        """
        if seq <= 0 or not event_id:
            raise ValueError("event sequence and event_id are required")
        if not bar.is_final:
            raise BacktestJournalError("only completed bars can enter a backtest journal")
        bar_json = canonical_json(bar)
        bar_hash = hashlib.sha256(bar_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq, checkpoint_json FROM backtest_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                connection.rollback()
                raise BacktestJournalError("backtest journal run has not been bound")
            if run["state"] == "PAUSED":
                connection.rollback()
                raise BacktestJournalError("backtest journal is paused and requires operator review")
            checkpoint = self._validated_checkpoint_or_pause(
                connection,
                run_id=run_id,
                seq=int(run["next_seq"]),
                checkpoint_json=run["checkpoint_json"],
            )
            existing = connection.execute(
                "SELECT * FROM backtest_event WHERE run_id=? AND seq=?", (run_id, seq)
            ).fetchone()
            if existing is not None:
                if existing["event_id"] != event_id or existing["bar_sha256"] != bar_hash or existing["bar_json"] != bar_json:
                    connection.execute("UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
                    connection.commit()
                    raise BacktestRunMismatch("bar identity or content changed during replay; run paused")
                if existing["result_json"] is not None:
                    result_json = existing["result_json"]
                    if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != existing["result_sha256"]:
                        connection.execute("UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
                        connection.commit()
                        raise BacktestJournalError("committed worker result integrity check failed; run paused")
                    result = WorkerEventResult.model_validate_json(result_json)
                    connection.commit()
                    return result, True
                if run["state"] != "RUNNING" or seq != int(run["next_seq"]):
                    if run["state"] == "RUNNING" and seq != int(run["next_seq"]):
                        connection.execute(
                            "UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                            (run_id,),
                        )
                        connection.commit()
                    else:
                        connection.rollback()
                    raise BacktestJournalError("uncommitted event is not at the active backtest cursor")
                connection.execute(
                    "UPDATE backtest_event SET released_at=COALESCE(released_at, CURRENT_TIMESTAMP) WHERE run_id=? AND seq=?",
                    (run_id, seq),
                )
            else:
                if run["state"] != "RUNNING" or seq != int(run["next_seq"]):
                    if run["state"] == "RUNNING" and seq != int(run["next_seq"]):
                        connection.execute(
                            "UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                            (run_id,),
                        )
                        connection.commit()
                    else:
                        connection.rollback()
                    raise BacktestJournalError("new event does not match the active backtest cursor")
                event_id_row = connection.execute(
                    "SELECT seq FROM backtest_event WHERE run_id=? AND event_id=?", (run_id, event_id)
                ).fetchone()
                if event_id_row is not None:
                    connection.rollback()
                    raise BacktestRunMismatch("event_id was already used by another sequence")
                connection.execute(
                    """INSERT INTO backtest_event
                       (run_id, seq, event_id, bar_json, bar_sha256, released_at)
                       VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (run_id, seq, event_id, bar_json, bar_hash),
                )
            connection.commit()

        # Worker execution is intentionally outside the SQLite transaction.
        # A crash leaves the released event at the cursor for an exact retry.
        from src.execution.event_worker import invoke_strategy_worker

        result = invoke_strategy_worker(sandbox=sandbox, source=source, event_id=event_id, bar=bar, state=checkpoint)
        result_json = canonical_json(result)
        result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq FROM backtest_run WHERE run_id=?", (run_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT event_id, bar_sha256, result_json, result_sha256 FROM backtest_event WHERE run_id=? AND seq=?",
                (run_id, seq),
            ).fetchone()
            if run is None or event is None or event["event_id"] != event_id or event["bar_sha256"] != bar_hash:
                connection.rollback()
                raise BacktestRunMismatch("event changed while worker was running")
            if event["result_json"] is not None:
                if event["result_sha256"] != result_hash:
                    connection.execute("UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
                    connection.commit()
                    raise BacktestRunMismatch("concurrent worker produced conflicting result; run paused")
                connection.commit()
                return WorkerEventResult.model_validate_json(event["result_json"]), True
            if run["state"] != "RUNNING" or int(run["next_seq"]) != seq:
                connection.rollback()
                raise BacktestJournalError("backtest cursor changed before event commit")
            connection.execute(
                """UPDATE backtest_event SET result_json=?, result_sha256=?, committed_at=CURRENT_TIMESTAMP
                   WHERE run_id=? AND seq=? AND result_json IS NULL""",
                (result_json, result_hash, run_id, seq),
            )
            connection.execute(
                """UPDATE backtest_run SET checkpoint_json=?, next_seq=next_seq+1,
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND next_seq=?""",
                (canonical_json(result.state), run_id, seq),
            )
            connection.commit()
        return result, False

    @staticmethod
    def _strict_json_object(raw: str) -> dict[str, Any]:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant: {value}")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("checkpoint/result JSON must be an object")
        return value

    @classmethod
    def _validated_checkpoint_or_pause(
        cls,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        seq: int,
        checkpoint_json: str,
    ) -> dict[str, Any]:
        """Validate the cursor state while the caller holds BEGIN IMMEDIATE.

        Any inconsistency is committed as PAUSED before returning control, so
        no strategy worker can be invoked with a forged or stale checkpoint.
        """
        try:
            checkpoint = cls._strict_json_object(checkpoint_json)
            if seq == 1:
                if checkpoint:
                    raise ValueError("initial checkpoint is not empty")
                return checkpoint

            prior = connection.execute(
                """SELECT event_id, result_json, result_sha256, committed_at
                   FROM backtest_event WHERE run_id=? AND seq=?""",
                (run_id, seq - 1),
            ).fetchone()
            if prior is None or prior["result_json"] is None or prior["committed_at"] is None:
                raise ValueError("prior committed event result is missing")
            result_json = prior["result_json"]
            result_sha = prior["result_sha256"]
            if not is_sha256(result_sha) or hashlib.sha256(result_json.encode("utf-8")).hexdigest() != result_sha:
                raise ValueError("prior committed event result integrity check failed")
            result_data = cls._strict_json_object(result_json)
            result = WorkerEventResult.model_validate(result_data)
            if result.event_id != prior["event_id"]:
                raise ValueError("prior committed event result identity mismatch")
            if canonical_json(checkpoint) != canonical_json(result.state):
                raise ValueError("checkpoint does not match prior committed event state")
            return checkpoint
        except Exception as exc:
            connection.execute(
                "UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.commit()
            raise BacktestJournalError(f"backtest checkpoint integrity check failed; run paused: {exc}") from exc

    def mark_complete(self, run_id: str, *, expected_events: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, next_seq FROM backtest_run WHERE run_id=?", (run_id,)).fetchone()
            if (
                row is None
                or row["state"] == "PAUSED"
                or int(row["next_seq"]) != expected_events + 1
            ):
                connection.rollback()
                raise BacktestJournalError("cannot complete before every expected event is committed")
            connection.execute(
                "UPDATE backtest_run SET state='COMPLETE', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.commit()

    def mark_paused(self, run_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE backtest_run SET state='PAUSED', updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,)
            )

    def get_status(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM backtest_run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise BacktestJournalError("backtest journal run does not exist")
            return {
                "run_id": row["run_id"],
                "state": row["state"],
                "next_seq": int(row["next_seq"]),
                "strategy_version_hash": row["strategy_version_hash"],
                "strategy_spec_hash": row["strategy_spec_hash"],
                "run_config_hash": row["run_config_hash"],
            }
