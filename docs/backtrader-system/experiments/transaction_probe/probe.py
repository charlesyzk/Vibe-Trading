#!/usr/bin/env python3
"""Offline SQLite probe for atomic event_result/checkpoint/intent acceptance.

This probe never opens a broker connection and never sends an order. An intent
row represents durable intent acceptance only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS deployments (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    paused INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO deployments(singleton, paused) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS event_results (
    event_seq INTEGER PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    previous_revision INTEGER NOT NULL,
    checkpoint_revision INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    commands_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
INSERT OR IGNORE INTO checkpoints(singleton, revision, state_json)
VALUES (1, 0, '{}');
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL UNIQUE REFERENCES event_results(event_seq),
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL REFERENCES event_results(event_seq),
    dedupe_key TEXT NOT NULL UNIQUE,
    command_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_seq INTEGER PRIMARY KEY REFERENCES event_results(event_seq),
    kind TEXT NOT NULL
);
"""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def result_payload(state: dict[str, Any], command: dict[str, Any], previous_revision: int) -> dict[str, Any]:
    return {
        "previous_checkpoint_revision": previous_revision,
        "state": state,
        "commands": [command],
    }


def submit(path: Path, payload: dict[str, Any], crash: str = "none") -> str:
    """Accept one result atomically. Crash modes exit without Python cleanup."""
    db = connect(path)
    payload_hash = digest(payload)
    event_seq = 41
    state_json = canonical(payload["state"])
    commands_json = canonical(payload["commands"])

    db.execute("BEGIN IMMEDIATE")
    existing = db.execute("SELECT payload_hash FROM event_results WHERE event_seq=?", (event_seq,)).fetchone()
    if existing:
        if existing["payload_hash"] == payload_hash:
            db.execute("COMMIT")
            db.close()
            return "duplicate_same_result"
        db.execute("UPDATE deployments SET paused=1 WHERE singleton=1")
        db.execute("COMMIT")
        db.close()
        return "conflict_paused"

    revision = db.execute("SELECT revision FROM checkpoints WHERE singleton=1").fetchone()["revision"]
    supplied_revision = payload["previous_checkpoint_revision"]
    if revision != supplied_revision:
        db.execute("ROLLBACK")
        db.close()
        return "stale_checkpoint_revision"

    next_revision = revision + 1
    db.execute(
        "INSERT INTO event_results VALUES (?, ?, ?, ?, ?, ?)",
        (event_seq, payload_hash, revision, next_revision, state_json, commands_json),
    )
    db.execute(
        "UPDATE checkpoints SET revision=?, state_json=? WHERE singleton=1",
        (next_revision, state_json),
    )
    db.execute("INSERT INTO decisions VALUES (?, ?, ?)", (f"decision-{event_seq}", event_seq, "probe signal"))
    for index, command in enumerate(payload["commands"]):
        db.execute(
            "INSERT INTO intents VALUES (?, ?, ?, ?)",
            (
                f"intent-{event_seq}-{index}",
                event_seq,
                f"deployment-v1/event-{event_seq}/action-{index}",
                canonical(command),
            ),
        )
    db.execute("INSERT INTO events VALUES (?, ?)", (event_seq, "EVENT_RESULT_ACCEPTED"))

    if crash == "before_commit":
        # Simulates abrupt process death. SQLite must roll back the open transaction.
        import os

        os._exit(73)
    db.execute("COMMIT")
    if crash == "after_commit":
        # Durable state exists, but caller never received the acceptance response.
        import os

        os._exit(74)
    db.close()
    return "accepted"


def counts(path: Path) -> dict[str, Any]:
    db = connect(path)
    tables = ("event_results", "checkpoints", "decisions", "intents", "events")
    result = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    result["checkpoint_revision"] = db.execute("SELECT revision FROM checkpoints WHERE singleton=1").fetchone()[0]
    result["paused"] = bool(db.execute("SELECT paused FROM deployments WHERE singleton=1").fetchone()[0])
    db.close()
    return result


def invoke(db: Path, payload: dict[str, Any], crash: str = "none") -> int:
    args = [sys.executable, str(Path(__file__).resolve()), "submit", str(db), canonical(payload), crash]
    return subprocess.run(args, check=False).returncode


def scenario() -> int:
    base = result_payload(
        {"sma_fast": 102.5, "position": 0},
        {"side": "BUY", "qty": 1, "symbol": "SYNTH"},
        previous_revision=0,
    )
    with tempfile.TemporaryDirectory(prefix="transaction-probe-") as tmp:
        root = Path(tmp)
        before_db = root / "before.sqlite"
        before_rc = invoke(before_db, base, "before_commit")
        before_state = counts(before_db)
        print(f"before_commit_exit={before_rc} state={canonical(before_state)}")

        after_db = root / "after.sqlite"
        after_rc = invoke(after_db, base, "after_commit")
        after_state = counts(after_db)
        retry_rc = invoke(after_db, base)
        retry_state = counts(after_db)
        print(f"after_commit_exit={after_rc} state={canonical(after_state)}")
        print(f"same_result_retry_exit={retry_rc} state={canonical(retry_state)}")

        conflict_db = root / "conflict.sqlite"
        conflict_first_rc = invoke(conflict_db, base)
        changed = result_payload(
            {"sma_fast": 102.5, "position": 1},
            {"side": "BUY", "qty": 1, "symbol": "SYNTH"},
            previous_revision=0,
        )
        conflict_rc = invoke(conflict_db, changed)
        conflict_state = counts(conflict_db)
        print(f"conflict_first_exit={conflict_first_rc} state={canonical(counts(conflict_db))}")
        print(f"different_result_retry_exit={conflict_rc} state={canonical(conflict_state)}")

        expected_before = {
            "event_results": 0, "checkpoints": 1, "decisions": 0, "intents": 0,
            "events": 0, "checkpoint_revision": 0, "paused": False,
        }
        expected_accepted = {
            "event_results": 1, "checkpoints": 1, "decisions": 1, "intents": 1,
            "events": 1, "checkpoint_revision": 1, "paused": False,
        }
        expected_conflict = {**expected_accepted, "paused": True}
        passed = (
            before_rc == 73 and before_state == expected_before
            and after_rc == 74 and after_state == expected_accepted
            and retry_rc == 0 and retry_state == expected_accepted
            and conflict_first_rc == 0 and conflict_rc == 0 and conflict_state == expected_conflict
        )
        print(f"scenario_result={'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("db", type=Path)
    submit_parser.add_argument("payload")
    submit_parser.add_argument("crash", choices=("none", "before_commit", "after_commit"))
    sub.add_parser("scenario")
    args = parser.parse_args()
    if args.command == "scenario":
        return scenario()
    result = submit(args.db, json.loads(args.payload), args.crash)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
