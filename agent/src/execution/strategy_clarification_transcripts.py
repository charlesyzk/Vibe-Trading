"""Durable, append-only provenance for NL2 strategy clarification turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class ClarificationTranscriptError(ValueError):
    """A transcript append is stale, conflicting, or malformed."""


@dataclass(frozen=True, slots=True)
class ClarificationTranscriptCursor:
    transcript_id: str
    turn_id: str
    turn_count: int
    strategy_description: str


class StrategyClarificationTranscriptStore:
    """Persist the exact input and user-provided answer at each preview turn.

    Turns are immutable and form a single-parent chain. A stable caller turn ID
    makes retries idempotent; reusing it with different content or appending to
    a stale parent is rejected. Editing an earlier answer starts a new transcript.
    """

    MAX_TURNS = 32
    MAX_DESCRIPTION_CHARS = 4096
    MAX_ANSWER_CHARS = 512

    def __init__(self, database: str | Path, *, now=None) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def append_initial(self, description: str, *, context_sha256: str) -> ClarificationTranscriptCursor:
        value = self._description(description)
        context_sha256 = self._context_hash(context_sha256)
        transcript_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        stamp = self._timestamp()
        digest = self._digest({"kind": "initial", "description": value})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO strategy_clarification_transcript"
                "(transcript_id, context_sha256, created_at, updated_at, head_turn_id, turn_count) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (transcript_id, context_sha256, stamp, stamp, turn_id),
            )
            db.execute(
                "INSERT INTO strategy_clarification_turn"
                "(transcript_id, turn_id, parent_turn_id, kind, answer, description_after, content_sha256, created_at) "
                "VALUES (?, ?, NULL, 'initial', NULL, ?, ?, ?)",
                (transcript_id, turn_id, value, digest, stamp),
            )
        return ClarificationTranscriptCursor(transcript_id, turn_id, 1, value)

    def append_clarification(
        self,
        *,
        transcript_id: str,
        turn_id: str,
        parent_turn_id: str,
        answer: str,
        submitted_description: str,
        context_sha256: str,
    ) -> ClarificationTranscriptCursor:
        transcript_id = self._uuid(transcript_id, "transcript_id")
        turn_id = self._uuid(turn_id, "turn_id")
        parent_turn_id = self._uuid(parent_turn_id, "parent_turn_id")
        answer = self._answer(answer)
        submitted_description = self._description(submitted_description)
        context_sha256 = self._context_hash(context_sha256)
        digest = self._digest({
            "kind": "clarification", "parent_turn_id": parent_turn_id,
            "answer": answer, "description_after": submitted_description,
        })
        expected = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            transcript = db.execute(
                "SELECT head_turn_id, turn_count, context_sha256 "
                "FROM strategy_clarification_transcript WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
            if transcript is None:
                raise ClarificationTranscriptError("clarification transcript was not found")
            if transcript[2] != context_sha256:
                raise ClarificationTranscriptError("clarification transcript belongs to a different backtest request")
            existing = db.execute(
                "SELECT content_sha256, description_after, parent_turn_id FROM strategy_clarification_turn "
                "WHERE transcript_id=? AND turn_id=?",
                (transcript_id, turn_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest or existing[1] != submitted_description or existing[2] != parent_turn_id:
                    raise ClarificationTranscriptError("clarification turn ID was reused with different content")
                if transcript[0] != turn_id:
                    raise ClarificationTranscriptError("clarification parent is stale; start a fresh preview")
                return ClarificationTranscriptCursor(transcript_id, turn_id, int(transcript[1]), submitted_description)
            head_turn_id, turn_count, _context_sha256 = transcript
            if head_turn_id != parent_turn_id:
                raise ClarificationTranscriptError("clarification parent is stale; start a fresh preview")
            if int(turn_count) >= self.MAX_TURNS:
                raise ClarificationTranscriptError("clarification transcript reached its turn limit")
            parent = db.execute(
                "SELECT description_after FROM strategy_clarification_turn WHERE transcript_id=? AND turn_id=?",
                (transcript_id, parent_turn_id),
            ).fetchone()
            if parent is None:
                raise ClarificationTranscriptError("clarification parent turn was not found")
            expected = f"{parent[0]}\n\nUser clarification: {answer}"
            if submitted_description != expected:
                raise ClarificationTranscriptError("submitted strategy description differs from the recorded turn chain")
            stamp = self._timestamp()
            new_count = int(turn_count) + 1
            db.execute(
                "INSERT INTO strategy_clarification_turn"
                "(transcript_id, turn_id, parent_turn_id, kind, answer, description_after, content_sha256, created_at) "
                "VALUES (?, ?, ?, 'clarification', ?, ?, ?, ?)",
                (transcript_id, turn_id, parent_turn_id, answer, submitted_description, digest, stamp),
            )
            db.execute(
                "UPDATE strategy_clarification_transcript SET updated_at=?, head_turn_id=?, turn_count=? "
                "WHERE transcript_id=?",
                (stamp, turn_id, new_count, transcript_id),
            )
        assert expected is not None
        return ClarificationTranscriptCursor(transcript_id, turn_id, new_count, expected)

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS strategy_clarification_transcript ("
                "transcript_id TEXT PRIMARY KEY, context_sha256 TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "head_turn_id TEXT NOT NULL, turn_count INTEGER NOT NULL CHECK(turn_count > 0));"
                "CREATE TABLE IF NOT EXISTS strategy_clarification_turn ("
                "transcript_id TEXT NOT NULL REFERENCES strategy_clarification_transcript(transcript_id), "
                "turn_id TEXT NOT NULL, parent_turn_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('initial','clarification')), "
                "answer TEXT, description_after TEXT NOT NULL, content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, "
                "PRIMARY KEY(transcript_id, turn_id));"
                "CREATE TRIGGER IF NOT EXISTS strategy_clarification_turn_no_update "
                "BEFORE UPDATE ON strategy_clarification_turn BEGIN SELECT RAISE(ABORT, 'turns are immutable'); END;"
                "CREATE TRIGGER IF NOT EXISTS strategy_clarification_turn_no_delete "
                "BEFORE DELETE ON strategy_clarification_turn BEGIN SELECT RAISE(ABORT, 'turns are immutable'); END;"
            )
        try:
            self.database.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ClarificationTranscriptError("transcript clock must return an aware timestamp")
        return value.astimezone(timezone.utc).isoformat()

    @classmethod
    def _description(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > cls.MAX_DESCRIPTION_CHARS:
            raise ClarificationTranscriptError("strategy description is empty or exceeds its length limit")
        return value.strip()

    @classmethod
    def _answer(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > cls.MAX_ANSWER_CHARS:
            raise ClarificationTranscriptError("clarification answer is empty or exceeds its length limit")
        return value.strip()

    @staticmethod
    def _context_hash(value: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ClarificationTranscriptError("backtest request context hash is invalid")
        return value

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ClarificationTranscriptError(f"{field} must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ClarificationTranscriptError(f"{field} must use canonical UUID form")
        return str(parsed)

    @staticmethod
    def _digest(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ClarificationTranscriptCursor", "ClarificationTranscriptError",
    "StrategyClarificationTranscriptStore",
]
