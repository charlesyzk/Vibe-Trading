"""Trusted, transactional handoff of one strategy event at a time.

This journal is an execution-service primitive, not an IPC transport. The
current coordinator uses a versioned one-shot JSON-over-stdio channel to a
network-isolated worker container. It passes only ``release_next_event``
output. Worker results cannot choose account, environment, actor, deployment,
version, or sequence; those values come from the stored run and the currently
outstanding event. A long-lived service socket and authenticated management
channel are still separate, unimplemented integration work.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, JsonValue, field_validator, model_validator

from src.execution.contracts import Environment, canonical_json, is_sha256
from src.execution.contracts.models import StrictModel


MAX_EVENT_BYTES = 1_048_576
MAX_RESULT_BYTES = 1_048_576
MAX_ORDER_PROPOSALS = 100


class WorkerOrderProposal(StrictModel):
    """Untrusted strategy request, deliberately lacking account authority."""

    side: str
    integer_qty: Annotated[int, Field(strict=True, gt=0)]
    order_type: str = "market"
    time_in_force: str = "DAY"
    reason: Annotated[str, Field(max_length=1_024)] | None = None

    @model_validator(mode="after")
    def validate_proposal(self) -> "WorkerOrderProposal":
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("proposal side must be BUY or SELL")
        if self.order_type != "market" or self.time_in_force != "DAY":
            raise ValueError("only Market/DAY proposals are supported")
        return self


class WorkerEventResult(StrictModel):
    """Minimal untrusted output bound to the currently released event."""

    event_id: str
    state: dict[str, JsonValue]
    order_proposals: tuple[WorkerOrderProposal, ...] = ()

    @field_validator("order_proposals")
    @classmethod
    def validate_proposal_count(cls, value: tuple[WorkerOrderProposal, ...]) -> tuple[WorkerOrderProposal, ...]:
        if len(value) > MAX_ORDER_PROPOSALS:
            raise ValueError("too many order proposals in one event result")
        return value


@dataclass(frozen=True, slots=True)
class ReleasedEvent:
    run_id: str
    event_id: str
    seq: int
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class StagedEvent:
    run_id: str
    event_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class CommittedEventRecord:
    seq: int
    event_id: str
    payload: dict[str, Any]
    result: WorkerEventResult
    payload_sha256: str
    result_sha256: str


@dataclass(frozen=True, slots=True)
class CommittedEventPrefix:
    run_id: str
    strategy_version_hash: str
    initial_checkpoint: dict[str, Any]
    committed_checkpoint: dict[str, Any]
    events: tuple[CommittedEventRecord, ...]


class JournalError(RuntimeError):
    """Base class for fail-closed journal errors."""


class RunUnavailable(JournalError):
    pass


class EventNotReady(JournalError):
    pass


class ResultConflict(JournalError):
    pass


class SQLiteEventJournal:
    """SQLite store for event release, checkpoint and order outbox commits."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = str(path)
        self.timeout_seconds = timeout_seconds
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS execution_run (
                    run_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper', 'live')),
                    strategy_version_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('RUNNING', 'PAUSED', 'STOPPED')),
                    next_seq INTEGER NOT NULL CHECK(next_seq > 0),
                    checkpoint_json TEXT NOT NULL,
                    initial_checkpoint_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS strategy_event (
                    run_id TEXT NOT NULL REFERENCES execution_run(run_id),
                    seq INTEGER NOT NULL CHECK(seq > 0),
                    event_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    released_at TEXT,
                    committed_at TEXT,
                    PRIMARY KEY(run_id, seq),
                    UNIQUE(run_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS event_result (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(run_id, seq),
                    FOREIGN KEY(run_id, seq) REFERENCES strategy_event(run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS order_outbox (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    proposal_index INTEGER NOT NULL,
                    proposal_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(status IN ('PENDING', 'RESERVED', 'SEND_STARTED', 'UNKNOWN', 'ACKNOWLEDGED', 'REJECTED')),
                    prepared_intent_json TEXT,
                    prepared_intent_sha256 TEXT,
                    intent_id TEXT,
                    decision_json TEXT,
                    decision_sha256 TEXT,
                    rejection_reason TEXT,
                    PRIMARY KEY(run_id, seq, proposal_index),
                    FOREIGN KEY(run_id, seq) REFERENCES event_result(run_id, seq)
                );
                """
            )
            # Additive migration for databases created before trusted outbox
            # dispatch was introduced.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(order_outbox)")}
            for name, sql_type in (
                ("prepared_intent_json", "TEXT"),
                ("prepared_intent_sha256", "TEXT"),
                ("intent_id", "TEXT"),
                ("decision_json", "TEXT"),
                ("decision_sha256", "TEXT"),
                ("rejection_reason", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE order_outbox ADD COLUMN {name} {sql_type}")
            # Legacy prepared rows predate these content hashes. Backfill the
            # exact stored bytes so subsequent reopen/replay detects partial
            # corruption; normal JSON/canonical validation still applies.
            for row in connection.execute(
                "SELECT run_id,seq,proposal_index,prepared_intent_json,decision_json "
                "FROM order_outbox WHERE prepared_intent_json IS NOT NULL AND decision_json IS NOT NULL "
                "AND (prepared_intent_sha256 IS NULL OR decision_sha256 IS NULL)"
            ).fetchall():
                connection.execute(
                    "UPDATE order_outbox SET prepared_intent_sha256=?, decision_sha256=? "
                    "WHERE run_id=? AND seq=? AND proposal_index=?",
                    (
                        hashlib.sha256(row["prepared_intent_json"].encode("utf-8")).hexdigest(),
                        hashlib.sha256(row["decision_json"].encode("utf-8")).hexdigest(),
                        row["run_id"], row["seq"], row["proposal_index"],
                    ),
                )
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(execution_run)")}
            if "initial_checkpoint_json" not in run_columns:
                # Historical runs did not retain the seed state. Leave them
                # NULL and fail closed if shadow replay is requested.
                connection.execute("ALTER TABLE execution_run ADD COLUMN initial_checkpoint_json TEXT")

    def create_run(
        self,
        *,
        run_id: str,
        deployment_id: str,
        account_ref: str,
        environment: Environment,
        strategy_version_hash: str,
        initial_checkpoint: dict[str, JsonValue] | None = None,
    ) -> None:
        if not is_sha256(strategy_version_hash):
            raise ValueError("strategy_version_hash must be a lowercase SHA-256")
        checkpoint_json = canonical_json(initial_checkpoint or {})
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO execution_run
                    (run_id, deployment_id, account_ref, environment, strategy_version_hash,
                     state, next_seq, checkpoint_json, initial_checkpoint_json)
                    VALUES (?, ?, ?, ?, ?, 'RUNNING', 1, ?, ?)""",
                    (run_id, deployment_id, account_ref, environment.value, strategy_version_hash,
                     checkpoint_json, checkpoint_json),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise JournalError("run_id already exists") from exc

    def get_run_identity(self, run_id: str, *, allow_paused: bool = False) -> dict[str, str]:
        """Read immutable strategy authority for a trusted coordinator.

        Worker requests deliberately omit this identity. It exists only so the
        host can verify that the pinned source bundle matches the run before
        releasing an event.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT deployment_id, account_ref, environment, strategy_version_hash, state "
                "FROM execution_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunUnavailable("run does not exist")
            if row["state"] != "RUNNING" and not (allow_paused and row["state"] == "PAUSED"):
                raise RunUnavailable("run is not running")
            return {
                "deployment_id": row["deployment_id"],
                "account_ref": row["account_ref"],
                "environment": row["environment"],
                "strategy_version_hash": row["strategy_version_hash"],
            }

    def require_linked_outbox_intent(
        self,
        run_id: str,
        seq: int,
        proposal_index: int,
        intent_id: str,
        *,
        intent: dict[str, Any],
        allow_paused: bool = False,
    ) -> dict[str, str]:
        """Verify that a ledger intent is the exact committed proposal handoff for this run.

        The check includes the immutable prepared intent and its event/result provenance;
        matching deployment/version alone cannot distinguish separate executions of a run.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT o.proposal_json, o.status, o.prepared_intent_json,
                          o.prepared_intent_sha256, o.decision_json, o.decision_sha256, o.intent_id,
                          r.deployment_id, r.account_ref, r.environment,
                          r.strategy_version_hash, r.state,
                          e.event_id, e.payload_json, e.payload_sha256, e.committed_at,
                          er.result_json, er.result_sha256
                   FROM order_outbox o
                   JOIN execution_run r ON r.run_id = o.run_id
                   JOIN strategy_event e ON e.run_id = o.run_id AND e.seq = o.seq
                   JOIN event_result er ON er.run_id = o.run_id AND er.seq = o.seq
                   WHERE o.run_id=? AND o.seq=? AND o.proposal_index=?""",
                (run_id, seq, proposal_index),
            ).fetchone()
        if row is None or (row["state"] != "RUNNING" and not (allow_paused and row["state"] == "PAUSED")):
            raise JournalError("active order has no committed outbox link in the current run")
        if (
            row["status"] != "RESERVED"
            or row["intent_id"] != intent_id
            or row["prepared_intent_json"] is None
            or row["committed_at"] is None
        ):
            raise JournalError("active order does not match a completed outbox intent handoff")
        try:
            payload = self._strict_json_object(row["payload_json"])
            result_value = self._strict_json_object(row["result_json"])
            prepared_intent = self._strict_json_object(row["prepared_intent_json"])
            prepared_decision = self._strict_json_object(row["decision_json"])
            result = WorkerEventResult.model_validate(result_value)
            proposal_matches = canonical_json(result.order_proposals[proposal_index]) == row["proposal_json"]
        except (ValueError, TypeError, IndexError) as exc:
            raise JournalError("active order outbox provenance is invalid") from exc
        if (
            hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != row["payload_sha256"]
            or hashlib.sha256(row["result_json"].encode("utf-8")).hexdigest() != row["result_sha256"]
            or hashlib.sha256(row["prepared_intent_json"].encode("utf-8")).hexdigest()
            != row["prepared_intent_sha256"]
            or hashlib.sha256(row["decision_json"].encode("utf-8")).hexdigest() != row["decision_sha256"]
            or canonical_json(prepared_intent) != row["prepared_intent_json"]
            or canonical_json(prepared_decision) != row["decision_json"]
            or result.event_id != row["event_id"]
            or not proposal_matches
            or canonical_json(prepared_intent) != canonical_json(intent)
        ):
            raise JournalError("active order outbox provenance differs from its ledger intent")
        return {
            "deployment_id": row["deployment_id"],
            "account_ref": row["account_ref"],
            "environment": row["environment"],
            "strategy_version_hash": row["strategy_version_hash"],
        }

    def pause_run(self, run_id: str) -> None:
        """Durably stop a run from releasing further strategy events."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE execution_run SET state = 'PAUSED' WHERE run_id = ? AND state = 'RUNNING'",
                (run_id,),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM execution_run WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise RunUnavailable("run does not exist")

    def read_committed_prefix(self, run_id: str) -> CommittedEventPrefix:
        """Read and integrity-check a paused run's entire committed event prefix.

        This is the read-only input for strategy shadow replay. Historical runs
        without a durable initial checkpoint and any gaps or altered records
        are rejected; integrity failures durably pause the run.
        """
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    """SELECT state, next_seq, strategy_version_hash, initial_checkpoint_json,
                              checkpoint_json
                       FROM execution_run WHERE run_id = ?""",
                    (run_id,),
                ).fetchone()
                if run is None:
                    connection.rollback()
                    raise RunUnavailable("run does not exist")
                if run["state"] not in {"RUNNING", "PAUSED"}:
                    connection.rollback()
                    raise RunUnavailable("committed-prefix shadow replay requires an active run")
                seq_end = run["next_seq"]
                if type(seq_end) is not int or seq_end < 1:
                    raise ValueError("run cursor is invalid")
                initial_json = run["initial_checkpoint_json"]
                if initial_json is None:
                    if seq_end != 1:
                        connection.rollback()
                        raise JournalError("run predates durable initial checkpoint storage")
                    # No event has committed, so the current checkpoint is
                    # still the only available seed and nothing is replayed.
                    initial_json = run["checkpoint_json"]
                checkpoint_json = run["checkpoint_json"]
                initial = self._strict_json_object(initial_json)
                checkpoint = self._strict_json_object(checkpoint_json)
                if canonical_json(initial) != initial_json or canonical_json(checkpoint) != checkpoint_json:
                    raise ValueError("checkpoint JSON is not canonical")
                rows = connection.execute(
                    """SELECT e.seq, e.event_id, e.payload_json, e.payload_sha256,
                              e.released_at, e.committed_at AS event_committed_at,
                              r.event_id AS result_event_id, r.result_json, r.result_sha256,
                              r.committed_at AS result_committed_at
                       FROM strategy_event e LEFT JOIN event_result r
                         ON r.run_id=e.run_id AND r.seq=e.seq
                       WHERE e.run_id=? AND e.seq<? ORDER BY e.seq""",
                    (run_id, seq_end),
                ).fetchall()
                if len(rows) != seq_end - 1:
                    raise ValueError("committed event prefix has missing or extra events")
                cursor_event = connection.execute(
                    "SELECT committed_at FROM strategy_event WHERE run_id=? AND seq=?",
                    (run_id, seq_end),
                ).fetchone()
                if connection.execute(
                    "SELECT 1 FROM strategy_event WHERE run_id=? AND seq>? LIMIT 1",
                    (run_id, seq_end),
                ).fetchone() is not None:
                    raise ValueError("strategy event exists beyond the authoritative cursor")
                if cursor_event is not None and cursor_event["committed_at"] is not None:
                    raise ValueError("authoritative cursor event is already committed")
                if connection.execute(
                    "SELECT 1 FROM event_result WHERE run_id=? AND seq>=? LIMIT 1",
                    (run_id, seq_end),
                ).fetchone() is not None:
                    raise ValueError("event result exists beyond the committed prefix")
                records: list[CommittedEventRecord] = []
                for expected_seq, row in enumerate(rows, start=1):
                    if (
                        row["seq"] != expected_seq
                        or row["released_at"] is None
                        or row["event_committed_at"] is None
                        or row["result_committed_at"] is None
                        or row["event_id"] != row["result_event_id"]
                    ):
                        raise ValueError("committed event prefix is incomplete or out of sequence")
                    payload_json = row["payload_json"]
                    result_json = row["result_json"]
                    if (
                        not is_sha256(row["payload_sha256"])
                        or hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != row["payload_sha256"]
                        or not is_sha256(row["result_sha256"])
                        or hashlib.sha256(result_json.encode("utf-8")).hexdigest() != row["result_sha256"]
                    ):
                        raise ValueError("committed event prefix hash validation failed")
                    payload = self._strict_json_object(payload_json)
                    result_data = self._strict_json_object(result_json)
                    result = WorkerEventResult.model_validate(result_data)
                    if (
                        canonical_json(payload) != payload_json
                        or canonical_json(result) != result_json
                        or result.event_id != row["event_id"]
                    ):
                        raise ValueError("committed event prefix JSON or identity is invalid")
                    outbox_rows = connection.execute(
                        "SELECT proposal_index, proposal_json FROM order_outbox "
                        "WHERE run_id=? AND seq=? ORDER BY proposal_index",
                        (run_id, expected_seq),
                    ).fetchall()
                    if len(outbox_rows) != len(result.order_proposals) or any(
                        outbox["proposal_index"] != index
                        or outbox["proposal_json"] != canonical_json(proposal)
                        for index, (outbox, proposal) in enumerate(zip(outbox_rows, result.order_proposals))
                    ):
                        raise ValueError("committed event outbox differs from its stored result")
                    records.append(CommittedEventRecord(
                        seq=expected_seq,
                        event_id=row["event_id"],
                        payload=payload,
                        result=result,
                        payload_sha256=row["payload_sha256"],
                        result_sha256=row["result_sha256"],
                    ))
                if records and canonical_json(checkpoint) != canonical_json(records[-1].result.state):
                    raise ValueError("committed checkpoint differs from final prefix result")
                connection.commit()
                return CommittedEventPrefix(
                    run_id=run_id,
                    strategy_version_hash=run["strategy_version_hash"],
                    initial_checkpoint=initial,
                    committed_checkpoint=checkpoint,
                    events=tuple(records),
                )
        except (RunUnavailable, JournalError):
            raise
        except Exception as exc:
            try:
                with closing(self._connect()) as connection:
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
            finally:
                raise JournalError("committed event prefix failed integrity validation") from exc

    def inspect_paused_released_event(self, run_id: str) -> ReleasedEvent:
        """Return the exact uncommitted released event of a paused run.

        This is a read-only recovery primitive. It never changes the run gate
        and verifies the canonical payload against its durable digest.
        """
        with closing(self._connect()) as connection:
            run = connection.execute(
                "SELECT state, next_seq FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "PAUSED":
                raise RunUnavailable("run is missing or not paused")
            row = connection.execute(
                """SELECT event_id, seq, payload_json, payload_sha256, released_at, committed_at
                   FROM strategy_event WHERE run_id = ? AND seq = ?""",
                (run_id, run["next_seq"]),
            ).fetchone()
            if row is None or row["released_at"] is None or row["committed_at"] is not None:
                raise EventNotReady("paused run has no released event awaiting recovery")
            if connection.execute(
                "SELECT 1 FROM event_result WHERE run_id = ? AND seq = ? LIMIT 1",
                (run_id, row["seq"]),
            ).fetchone() is not None:
                raise JournalError("paused event already has a result")
            if connection.execute(
                "SELECT 1 FROM order_outbox WHERE run_id = ? AND seq = ? LIMIT 1",
                (run_id, row["seq"]),
            ).fetchone() is not None:
                raise JournalError("paused event already has order outbox work")
            if connection.execute(
                "SELECT 1 FROM strategy_event WHERE run_id = ? AND seq > ? LIMIT 1",
                (run_id, row["seq"]),
            ).fetchone() is not None:
                raise JournalError("paused event has later staged events")
            self._validate_retry_checkpoint(connection, run_id, int(row["seq"]))
        try:
            payload = self._strict_json_object(row["payload_json"])
            if canonical_json(payload) != row["payload_json"]:
                raise ValueError("payload JSON is not canonical")
        except (TypeError, ValueError) as exc:
            raise JournalError("paused event payload is invalid") from exc
        digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise JournalError("paused event payload integrity check failed")
        return ReleasedEvent(
            run_id=run_id,
            event_id=row["event_id"],
            seq=int(row["seq"]),
            payload=payload,
            payload_sha256=digest,
        )

    def resume_paused_released_event(
        self,
        run_id: str,
        *,
        expected_event_id: str,
        expected_seq: int,
        expected_payload_sha256: str,
    ) -> ReleasedEvent:
        """Reopen only the same released event after trusted recovery checks.

        The recovery orchestrator must first reconcile the broker, restore its
        native state, and retain the owner lease. This journal method enforces
        the durable half of that contract: PAUSED→RUNNING is atomic and only
        succeeds for the caller's exact cursor identity, with no result/outbox
        side effects at that cursor. It does not authorize a public/API caller.
        """
        if (
            not isinstance(expected_event_id, str)
            or not expected_event_id
            or type(expected_seq) is not int
            or expected_seq < 1
            or not is_sha256(expected_payload_sha256)
        ):
            raise ValueError("expected paused event identity is invalid")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq, account_ref, environment FROM execution_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None or run["state"] != "PAUSED" or run["next_seq"] != expected_seq:
                connection.rollback()
                raise RunUnavailable("run is not paused at the expected event cursor")
            control_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_control'"
            ).fetchone()
            if control_table is not None:
                control_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(execution_control)")
                }
                if not {"broker", "account_ref", "environment", "state"}.issubset(control_columns):
                    connection.rollback()
                    raise JournalError("execution_control table has an unsupported schema")
                control = connection.execute(
                    "SELECT state FROM execution_control WHERE broker='IBKR' AND account_ref=? AND environment=?",
                    (run["account_ref"], run["environment"]),
                ).fetchone()
                if control is None or control["state"] != "RUNNING":
                    connection.rollback()
                    raise RunUnavailable("execution scope is not running")
            row = connection.execute(
                """SELECT event_id, seq, payload_json, payload_sha256, released_at, committed_at
                   FROM strategy_event WHERE run_id = ? AND seq = ?""",
                (run_id, expected_seq),
            ).fetchone()
            if (
                row is None
                or row["event_id"] != expected_event_id
                or row["payload_sha256"] != expected_payload_sha256
                or row["released_at"] is None
                or row["committed_at"] is not None
            ):
                connection.rollback()
                raise JournalError("paused event differs from the expected released event")
            if connection.execute(
                "SELECT 1 FROM event_result WHERE run_id = ? AND seq = ? LIMIT 1",
                (run_id, expected_seq),
            ).fetchone() is not None or connection.execute(
                "SELECT 1 FROM order_outbox WHERE run_id = ? AND seq = ? LIMIT 1",
                (run_id, expected_seq),
            ).fetchone() is not None:
                connection.rollback()
                raise JournalError("paused event has result or outbox side effects")
            if connection.execute(
                "SELECT 1 FROM strategy_event WHERE run_id = ? AND seq > ? LIMIT 1",
                (run_id, expected_seq),
            ).fetchone() is not None:
                connection.rollback()
                raise JournalError("paused event has later staged events")
            try:
                self._validate_retry_checkpoint(connection, run_id, expected_seq)
            except JournalError:
                connection.rollback()
                raise
            try:
                payload = self._strict_json_object(row["payload_json"])
                if canonical_json(payload) != row["payload_json"]:
                    raise ValueError("payload JSON is not canonical")
            except (TypeError, ValueError) as exc:
                connection.rollback()
                raise JournalError("paused event payload is invalid") from exc
            digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
            if digest != expected_payload_sha256:
                connection.rollback()
                raise JournalError("paused event payload integrity check failed")
            cursor = connection.execute(
                "UPDATE execution_run SET state = 'RUNNING' WHERE run_id = ? AND state = 'PAUSED' AND next_seq = ?",
                (run_id, expected_seq),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RunUnavailable("paused event cursor changed during recovery")
            connection.commit()
            return ReleasedEvent(
                run_id=run_id,
                event_id=row["event_id"],
                seq=int(row["seq"]),
                payload=payload,
                payload_sha256=digest,
            )

    @classmethod
    def _validate_retry_checkpoint(
        cls, connection: sqlite3.Connection, run_id: str, seq: int,
    ) -> None:
        """Verify the checkpoint is exactly the prefix before the retry cursor."""
        run = connection.execute(
            "SELECT checkpoint_json FROM execution_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise RunUnavailable("run does not exist")
        try:
            checkpoint = cls._strict_json_object(run["checkpoint_json"])
            if canonical_json(checkpoint) != run["checkpoint_json"]:
                raise ValueError("checkpoint JSON is not canonical")
        except (TypeError, ValueError) as exc:
            raise JournalError("paused event checkpoint is invalid") from exc
        if seq == 1:
            if checkpoint:
                raise JournalError("initial retry checkpoint is not empty")
            return
        prior = connection.execute(
            """SELECT e.event_id, e.committed_at AS event_committed_at,
                      r.event_id AS result_event_id, r.result_json, r.result_sha256,
                      r.committed_at AS result_committed_at
               FROM strategy_event e
               LEFT JOIN event_result r ON r.run_id = e.run_id AND r.seq = e.seq
               WHERE e.run_id = ? AND e.seq = ?""",
            (run_id, seq - 1),
        ).fetchone()
        if (
            prior is None
            or prior["event_committed_at"] is None
            or prior["result_committed_at"] is None
            or prior["result_json"] is None
            or prior["event_id"] != prior["result_event_id"]
            or not is_sha256(prior["result_sha256"])
            or hashlib.sha256(prior["result_json"].encode("utf-8")).hexdigest()
            != prior["result_sha256"]
        ):
            raise JournalError("paused event prior checkpoint result is missing or invalid")
        try:
            result = WorkerEventResult.model_validate(cls._strict_json_object(prior["result_json"]))
            if canonical_json(result) != prior["result_json"] or result.event_id != prior["event_id"]:
                raise ValueError("prior result identity or encoding differs")
        except (TypeError, ValueError) as exc:
            raise JournalError("paused event prior checkpoint result is invalid") from exc
        if result.state != checkpoint:
            raise JournalError("paused event checkpoint differs from the prior committed result")

    def stage_next_event(self, run_id: str, payload: dict[str, JsonValue], *, event_id: str | None = None) -> StagedEvent:
        """Persist the next host-authored event; it cannot be overwritten."""
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("event payload exceeds the configured byte limit")
        event_id = event_id or str(uuid.uuid4())
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "RUNNING":
                connection.rollback()
                raise RunUnavailable("run is missing or not running")
            outstanding = connection.execute(
                "SELECT seq FROM strategy_event WHERE run_id = ? AND committed_at IS NULL",
                (run_id,),
            ).fetchone()
            if outstanding is not None:
                connection.rollback()
                raise EventNotReady("the outstanding event must commit before another event is staged")
            seq = int(run["next_seq"])
            connection.execute(
                """INSERT INTO strategy_event(run_id, seq, event_id, payload_json, payload_sha256)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, seq, event_id, payload_json, payload_hash),
            )
            connection.commit()
        return StagedEvent(run_id=run_id, event_id=event_id, seq=seq)

    def peek_next_event(self, run_id: str) -> ReleasedEvent:
        """Read the authoritative cursor event without releasing it to a worker."""
        with closing(self._connect()) as connection:
            run = connection.execute(
                "SELECT state, next_seq FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "RUNNING":
                raise RunUnavailable("run is missing or not running")
            row = connection.execute(
                "SELECT event_id, seq, payload_json, payload_sha256 FROM strategy_event "
                "WHERE run_id = ? AND seq = ?",
                (run_id, run["next_seq"]),
            ).fetchone()
            if row is None:
                raise EventNotReady("no event is staged at the authoritative cursor")
            payload = json.loads(row["payload_json"])
            if hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise JournalError("staged event integrity check failed")
            return ReleasedEvent(
                run_id=run_id, event_id=row["event_id"], seq=int(row["seq"]),
                payload=payload, payload_sha256=row["payload_sha256"],
            )

    def release_next_event(self, run_id: str) -> ReleasedEvent:
        """Release only the host-staged event at the authoritative run cursor."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "RUNNING":
                connection.rollback()
                raise RunUnavailable("run is missing or not running")
            row = connection.execute(
                "SELECT event_id, seq, payload_json, payload_sha256, released_at FROM strategy_event "
                "WHERE run_id = ? AND seq = ?",
                (run_id, run["next_seq"]),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise EventNotReady("no event is staged at the authoritative cursor")
            payload = json.loads(row["payload_json"])
            if hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != row["payload_sha256"]:
                connection.execute("UPDATE execution_run SET state = 'PAUSED' WHERE run_id = ?", (run_id,))
                connection.commit()
                raise JournalError("staged event integrity check failed; run paused")
            if row["released_at"] is None:
                connection.execute(
                    "UPDATE strategy_event SET released_at = CURRENT_TIMESTAMP WHERE run_id = ? AND seq = ?",
                    (run_id, row["seq"]),
                )
            connection.commit()
            return ReleasedEvent(
                run_id=run_id,
                event_id=row["event_id"],
                seq=int(row["seq"]),
                payload=payload,
                payload_sha256=row["payload_sha256"],
            )

    def commit_result(self, run_id: str, worker_result: WorkerEventResult | dict[str, Any]) -> bool:
        """Atomically commit one event result, checkpoint and outbox proposals.

        Returns ``True`` for a new commit and ``False`` for an identical replay.
        A conflicting replay pauses the run and raises ``ResultConflict``.
        """
        result = worker_result if isinstance(worker_result, WorkerEventResult) else WorkerEventResult.model_validate(worker_result)
        result_json = canonical_json(result)
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("worker result exceeds the configured byte limit")
        result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT state, next_seq FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["state"] != "RUNNING":
                connection.rollback()
                raise RunUnavailable("run is missing or not running")
            existing = connection.execute(
                "SELECT event_id, result_sha256 FROM event_result WHERE run_id = ? AND event_id = ?",
                (run_id, result.event_id),
            ).fetchone()
            if existing is not None:
                if existing["result_sha256"] == result_hash:
                    connection.rollback()
                    return False
                connection.execute("UPDATE execution_run SET state = 'PAUSED' WHERE run_id = ?", (run_id,))
                connection.commit()
                raise ResultConflict("conflicting result replay paused the run")
            event = connection.execute(
                "SELECT event_id, seq, released_at, committed_at FROM strategy_event "
                "WHERE run_id = ? AND seq = ?",
                (run_id, run["next_seq"]),
            ).fetchone()
            if event is None or event["released_at"] is None:
                connection.rollback()
                raise EventNotReady("no released event is awaiting a result")
            if result.event_id != event["event_id"]:
                connection.execute("UPDATE execution_run SET state = 'PAUSED' WHERE run_id = ?", (run_id,))
                connection.commit()
                raise ResultConflict("worker result event_id does not match the released event")

            connection.execute(
                """INSERT INTO event_result(run_id, seq, event_id, result_json, result_sha256)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, event["seq"], result.event_id, result_json, result_hash),
            )
            for index, proposal in enumerate(result.order_proposals):
                connection.execute(
                    "INSERT INTO order_outbox(run_id, seq, proposal_index, proposal_json) VALUES (?, ?, ?, ?)",
                    (run_id, event["seq"], index, canonical_json(proposal)),
                )
            connection.execute(
                """UPDATE execution_run SET checkpoint_json = ?, next_seq = next_seq + 1
                   WHERE run_id = ? AND next_seq = ?""",
                (canonical_json(result.state), run_id, event["seq"]),
            )
            connection.execute(
                "UPDATE strategy_event SET committed_at = CURRENT_TIMESTAMP WHERE run_id = ? AND seq = ?",
                (run_id, event["seq"]),
            )
            connection.commit()
            return True

    def get_checkpoint(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, next_seq, checkpoint_json FROM execution_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RunUnavailable("run does not exist")
            try:
                checkpoint = self._strict_json_object(row["checkpoint_json"])
                if canonical_json(checkpoint) != row["checkpoint_json"]:
                    raise ValueError("checkpoint JSON is not canonical")
                if type(row["next_seq"]) is not int:
                    raise ValueError("event cursor is not an integer")
                seq = row["next_seq"]
                if seq < 1:
                    raise ValueError("event cursor is invalid")
                premature_result = connection.execute(
                    "SELECT 1 FROM event_result WHERE run_id = ? AND seq >= ? LIMIT 1",
                    (run_id, seq),
                ).fetchone()
                if premature_result is not None:
                    raise ValueError("event result exists at or beyond the active cursor")
                if seq == 1:
                    connection.commit()
                    return checkpoint

                prior = connection.execute(
                    """SELECT e.event_id AS event_id, e.committed_at AS event_committed_at,
                              r.event_id AS result_event_id, r.result_json, r.result_sha256,
                              r.committed_at AS result_committed_at
                       FROM strategy_event e
                       LEFT JOIN event_result r ON r.run_id = e.run_id AND r.seq = e.seq
                       WHERE e.run_id = ? AND e.seq = ?""",
                    (run_id, seq - 1),
                ).fetchone()
                if (
                    prior is None
                    or prior["event_committed_at"] is None
                    or prior["result_committed_at"] is None
                    or prior["result_json"] is None
                    or prior["event_id"] != prior["result_event_id"]
                ):
                    raise ValueError("prior committed event result is missing or has a different event identity")
                result_json = prior["result_json"]
                result_sha256 = prior["result_sha256"]
                if (
                    not is_sha256(result_sha256)
                    or hashlib.sha256(result_json.encode("utf-8")).hexdigest() != result_sha256
                ):
                    raise ValueError("prior committed event result hash is invalid")
                result_data = self._strict_json_object(result_json)
                result = WorkerEventResult.model_validate(result_data)
                if canonical_json(result) != result_json:
                    raise ValueError("prior committed event result JSON is not canonical")
                if result.event_id != prior["event_id"]:
                    raise ValueError("prior committed event result identity mismatch")
                if canonical_json(checkpoint) != canonical_json(result.state):
                    raise ValueError("checkpoint does not match prior committed event state")
                connection.commit()
                return checkpoint
            except Exception as exc:
                connection.execute(
                    "UPDATE execution_run SET state = 'PAUSED' WHERE run_id = ?", (run_id,)
                )
                connection.commit()
                raise JournalError(
                    f"execution checkpoint integrity check failed; run paused: {exc}"
                ) from exc

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

        value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(value, dict):
            raise ValueError("checkpoint/result JSON must be an object")
        return value

    def list_pending_outbox(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT seq, proposal_index, proposal_json, status FROM order_outbox "
                "WHERE run_id = ? AND status = 'PENDING' ORDER BY seq, proposal_index",
                (run_id,),
            ).fetchall()
            return [
                {
                    "seq": int(row["seq"]),
                    "proposal_index": int(row["proposal_index"]),
                    "proposal": json.loads(row["proposal_json"]),
                    "status": row["status"],
                }
                for row in rows
            ]

    def list_dispatchable_outbox(self, run_id: str, *, include_paused: bool = False) -> list[dict[str, Any]]:
        """Return unprepared proposals and prepared cross-store handoffs.

        RESERVED rows with no linked intent are recovery work: the immutable
        prepared intent must be replayed into the order ledger before the row
        can be considered handed off.
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT o.seq, o.proposal_index, o.proposal_json, o.status,
                          o.prepared_intent_json, o.prepared_intent_sha256,
                          o.intent_id, o.decision_json, o.decision_sha256,
                          r.deployment_id, r.account_ref, r.environment, r.strategy_version_hash,
                          e.payload_json, e.payload_sha256, e.event_id,
                          er.result_json, er.result_sha256
                   FROM order_outbox o
                   JOIN execution_run r ON r.run_id = o.run_id
                   JOIN strategy_event e ON e.run_id = o.run_id AND e.seq = o.seq
                   JOIN event_result er ON er.run_id = o.run_id AND er.seq = o.seq
                   WHERE o.run_id = ? AND r.state IN ('RUNNING', 'PAUSED') AND
                     (? = 1 OR r.state = 'RUNNING') AND
                     (o.status = 'PENDING' OR (o.status = 'RESERVED' AND o.intent_id IS NULL))
                   ORDER BY o.seq, o.proposal_index""",
                (run_id, int(include_paused)),
            ).fetchall()
            values = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    result_value = json.loads(row["result_json"])
                except (json.JSONDecodeError, TypeError) as exc:
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
                    connection.commit()
                    raise JournalError("journal JSON integrity check failed during outbox authorization; run paused") from exc
                if row["status"] == "RESERVED" and (
                    row["prepared_intent_json"] is None
                    or row["decision_json"] is None
                    or hashlib.sha256(row["prepared_intent_json"].encode("utf-8")).hexdigest()
                    != row["prepared_intent_sha256"]
                    or hashlib.sha256(row["decision_json"].encode("utf-8")).hexdigest()
                    != row["decision_sha256"]
                ):
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
                    connection.commit()
                    raise JournalError("prepared outbox intent integrity check failed; run paused")
                payload_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
                result_hash = hashlib.sha256(row["result_json"].encode("utf-8")).hexdigest()
                if payload_hash != row["payload_sha256"] or result_hash != row["result_sha256"]:
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
                    connection.commit()
                    raise JournalError("staged event or result integrity check failed during outbox authorization; run paused")
                try:
                    validated_result = WorkerEventResult.model_validate(result_value)
                    proposal_index = int(row["proposal_index"])
                    result_proposal = validated_result.order_proposals[proposal_index]
                    result_event_id = validated_result.event_id
                    proposal_matches = canonical_json(result_proposal) == row["proposal_json"]
                except (IndexError, ValueError, TypeError) as exc:
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
                    connection.commit()
                    raise JournalError("outbox proposal integrity check failed; run paused") from exc
                if result_event_id != row["event_id"] or not proposal_matches:
                    connection.execute(
                        "UPDATE execution_run SET state='PAUSED' WHERE run_id=?", (run_id,)
                    )
                    connection.commit()
                    raise JournalError("outbox proposal does not match its committed result; run paused")
                values.append({
                    "seq": int(row["seq"]),
                    "proposal_index": int(row["proposal_index"]),
                    "proposal": json.loads(row["proposal_json"]),
                    "status": row["status"],
                    "prepared_intent": json.loads(row["prepared_intent_json"])
                    if row["prepared_intent_json"] else None,
                    "intent_id": row["intent_id"],
                    "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
                    "identity": {
                        "deployment_id": row["deployment_id"],
                        "account_ref": row["account_ref"],
                        "environment": row["environment"],
                        "strategy_version_hash": row["strategy_version_hash"],
                    },
                    "event_id": row["event_id"],
                    "event_payload": payload,
                })
            connection.commit()
            return values

    def prepare_outbox_intent(
        self, run_id: str, seq: int, proposal_index: int, *, intent: dict[str, Any], decision: dict[str, Any]
    ) -> dict[str, Any]:
        """Freeze the approved intent before writing it to the separate ledger.

        Repeated preparation returns the first durable payload. A conflicting
        attempt is rejected so a fresh quote or worker replay cannot rewrite
        the authorization decision during crash recovery.
        """
        intent_json = canonical_json(intent)
        decision_json = canonical_json(decision)
        intent_hash = hashlib.sha256(intent_json.encode("utf-8")).hexdigest()
        decision_hash = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, prepared_intent_json, prepared_intent_sha256, "
                "decision_json, decision_sha256, intent_id FROM order_outbox "
                "WHERE run_id=? AND seq=? AND proposal_index=?",
                (run_id, seq, proposal_index),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise JournalError("outbox proposal does not exist")
            if row["status"] == "RESERVED":
                if (
                    row["prepared_intent_json"] != intent_json
                    or row["decision_json"] != decision_json
                    or row["prepared_intent_sha256"] != intent_hash
                    or row["decision_sha256"] != decision_hash
                ):
                    connection.rollback()
                    raise ResultConflict("prepared outbox intent is immutable")
                connection.commit()
                return {"intent": json.loads(intent_json), "decision": json.loads(decision_json), "intent_id": row["intent_id"]}
            if row["status"] != "PENDING":
                connection.rollback()
                raise EventNotReady("outbox proposal is no longer pending")
            connection.execute(
                "UPDATE order_outbox SET status='RESERVED', prepared_intent_json=?, "
                "prepared_intent_sha256=?, decision_json=?, decision_sha256=? "
                "WHERE run_id=? AND seq=? AND proposal_index=? AND status='PENDING'",
                (intent_json, intent_hash, decision_json, decision_hash, run_id, seq, proposal_index),
            )
            connection.commit()
        return {"intent": json.loads(intent_json), "decision": json.loads(decision_json), "intent_id": None}

    def link_outbox_intent(self, run_id: str, seq: int, proposal_index: int, intent_id: str) -> None:
        """Complete the idempotent journal-to-ledger handoff."""
        if not intent_id:
            raise ValueError("intent_id is required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, intent_id, prepared_intent_json FROM order_outbox "
                "WHERE run_id=? AND seq=? AND proposal_index=?",
                (run_id, seq, proposal_index),
            ).fetchone()
            if row is None or row["status"] != "RESERVED" or row["prepared_intent_json"] is None:
                connection.rollback()
                raise EventNotReady("outbox proposal has no prepared intent")
            if row["intent_id"] not in (None, intent_id):
                connection.rollback()
                raise ResultConflict("outbox proposal is linked to a different intent")
            connection.execute(
                "UPDATE order_outbox SET intent_id=? WHERE run_id=? AND seq=? AND proposal_index=?",
                (intent_id, run_id, seq, proposal_index),
            )
            connection.commit()

    def reject_outbox(self, run_id: str, seq: int, proposal_index: int, *, reason: str, decision: dict[str, Any]) -> None:
        """Durably finish a risk-rejected proposal without creating an intent."""
        if not reason or len(reason) > 2000:
            raise ValueError("rejection reason must contain 1 to 2000 characters")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, decision_json FROM order_outbox WHERE run_id=? AND seq=? AND proposal_index=?",
                (run_id, seq, proposal_index),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise JournalError("outbox proposal does not exist")
            decision_json = canonical_json(decision)
            if row["status"] == "REJECTED":
                if row["decision_json"] != decision_json:
                    connection.rollback()
                    raise ResultConflict("risk rejection is immutable")
                connection.commit()
                return
            if row["status"] != "PENDING":
                connection.rollback()
                raise EventNotReady("prepared outbox proposal cannot be rejected")
            connection.execute(
                "UPDATE order_outbox SET status='REJECTED', decision_json=?, rejection_reason=? "
                "WHERE run_id=? AND seq=? AND proposal_index=?",
                (decision_json, reason, run_id, seq, proposal_index),
            )
            connection.commit()
