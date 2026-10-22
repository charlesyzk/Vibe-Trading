from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import uuid

import pytest

from src.execution.strategy_clarification_transcripts import (
    ClarificationTranscriptError,
    StrategyClarificationTranscriptStore,
)

CONTEXT_HASH = "a" * 64


def test_transcript_append_is_durable_idempotent_and_reconstructs_exact_description(tmp_path):
    database = tmp_path / "clarifications.sqlite3"
    store = StrategyClarificationTranscriptStore(
        database, now=lambda: datetime(2026, 10, 22, 12, tzinfo=timezone.utc),
    )
    initial = store.append_initial("请用 SMA 金叉买入", context_sha256=CONTEXT_HASH)
    answer = "快线 5 日，慢线 20 日，反向死叉卖出"
    description = f"{initial.strategy_description}\n\nUser clarification: {answer}"
    turn_id = str(uuid.uuid4())
    turn = store.append_clarification(
        transcript_id=initial.transcript_id, turn_id=turn_id,
        parent_turn_id=initial.turn_id, answer=answer, submitted_description=description,
        context_sha256=CONTEXT_HASH,
    )
    reopened = StrategyClarificationTranscriptStore(database)
    replay = reopened.append_clarification(
        transcript_id=initial.transcript_id, turn_id=turn_id,
        parent_turn_id=initial.turn_id, answer=answer, submitted_description=description,
        context_sha256=CONTEXT_HASH,
    )
    assert replay == turn
    assert replay.turn_count == 2
    assert replay.strategy_description == description


def test_transcript_rejects_stale_parent_and_description_not_derived_from_turns(tmp_path):
    store = StrategyClarificationTranscriptStore(tmp_path / "clarifications.sqlite3")
    initial = store.append_initial("strategy", context_sha256=CONTEXT_HASH)
    current = store.append_clarification(
        transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
        parent_turn_id=initial.turn_id, answer="first answer",
        submitted_description="strategy\n\nUser clarification: first answer",
        context_sha256=CONTEXT_HASH,
    )
    with pytest.raises(ClarificationTranscriptError, match="parent is stale"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=initial.turn_id, answer="stale answer",
            submitted_description="strategy\n\nUser clarification: stale answer",
            context_sha256=CONTEXT_HASH,
        )
    with pytest.raises(ClarificationTranscriptError, match="differs from the recorded"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=current.turn_id, answer="new answer", submitted_description="forged full text",
            context_sha256=CONTEXT_HASH,
        )


def test_transcript_turn_id_cannot_be_reused_with_changed_content(tmp_path):
    store = StrategyClarificationTranscriptStore(tmp_path / "clarifications.sqlite3")
    initial = store.append_initial("strategy", context_sha256=CONTEXT_HASH)
    turn_id = str(uuid.uuid4())
    store.append_clarification(
        transcript_id=initial.transcript_id, turn_id=turn_id,
        parent_turn_id=initial.turn_id, answer="answer",
        submitted_description="strategy\n\nUser clarification: answer",
        context_sha256=CONTEXT_HASH,
    )
    with pytest.raises(ClarificationTranscriptError, match="reused with different content"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=turn_id,
            parent_turn_id=initial.turn_id, answer="changed",
            submitted_description="strategy\n\nUser clarification: changed",
            context_sha256=CONTEXT_HASH,
        )


def test_transcript_cannot_be_reused_for_a_different_market_or_budget_context(tmp_path):
    store = StrategyClarificationTranscriptStore(tmp_path / "clarifications.sqlite3")
    initial = store.append_initial("strategy", context_sha256=CONTEXT_HASH)
    with pytest.raises(ClarificationTranscriptError, match="different backtest request"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=initial.turn_id, answer="answer",
            submitted_description="strategy\n\nUser clarification: answer",
            context_sha256="b" * 64,
        )


def test_transcript_turn_log_is_append_only_and_database_permissions_are_private(tmp_path):
    database = tmp_path / "clarifications.sqlite3"
    store = StrategyClarificationTranscriptStore(database)
    initial = store.append_initial("strategy", context_sha256=CONTEXT_HASH)
    assert database.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE strategy_clarification_turn SET answer='changed' "
                "WHERE transcript_id=? AND turn_id=?", (initial.transcript_id, initial.turn_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "DELETE FROM strategy_clarification_turn WHERE transcript_id=? AND turn_id=?",
                (initial.transcript_id, initial.turn_id),
            )


def test_transcript_rejects_bad_ids_empty_answers_and_overlong_chains(tmp_path):
    store = StrategyClarificationTranscriptStore(tmp_path / "clarifications.sqlite3")
    initial = store.append_initial("strategy", context_sha256=CONTEXT_HASH)
    with pytest.raises(ClarificationTranscriptError, match="UUID"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id="bad", parent_turn_id=initial.turn_id,
            answer="answer", submitted_description="strategy\n\nUser clarification: answer",
            context_sha256=CONTEXT_HASH,
        )
    with pytest.raises(ClarificationTranscriptError, match="empty or exceeds"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=initial.turn_id, answer=" ", submitted_description="strategy",
            context_sha256=CONTEXT_HASH,
        )
    cursor = initial
    for index in range(store.MAX_TURNS - 1):
        answer = f"answer {index}"
        cursor = store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=cursor.turn_id, answer=answer,
            submitted_description=f"{cursor.strategy_description}\n\nUser clarification: {answer}",
            context_sha256=CONTEXT_HASH,
        )
    with pytest.raises(ClarificationTranscriptError, match="turn limit"):
        store.append_clarification(
            transcript_id=initial.transcript_id, turn_id=str(uuid.uuid4()),
            parent_turn_id=cursor.turn_id, answer="extra",
            submitted_description=f"{cursor.strategy_description}\n\nUser clarification: extra",
            context_sha256=CONTEXT_HASH,
        )
