from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.contracts import Environment
from src.execution.event_journal import RunUnavailable, SQLiteEventJournal, WorkerEventResult, WorkerOrderProposal
from src.execution.ibkr_callback_pump import BrokerOrderBinding, BrokerOrderIndex
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator
from src.execution.ibkr_reconciliation import SQLiteIbkrSnapshotStore
from src.execution.order_ledger import (
    BrokerEvent,
    BrokerEventKind,
    ExecutionControlState,
    OrderIntentInput,
    SQLiteOrderLedger,
)
from src.execution.sqlite_backup import (
    ExecutionDatabaseBackupError,
    apply_atomic_execution_database_migration,
    create_execution_database_backup,
    restore_execution_database_backup,
    verify_execution_database_backup,
)


NOW = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)


def _populated_execution_db(path):
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    SQLiteIbkrSnapshotStore(path)
    journal = SQLiteEventJournal(path)

    order = ledger.create_intent(OrderIntentInput(
        decision_id="backup-decision",
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        deployment_id="deployment-1",
        strategy_version_hash="a" * 64,
        input_event_seq=1,
        action_index=0,
        bar_end=NOW,
        instrument_id="XYZ",
        side="BUY",
        integer_qty=4,
        reference_price=Decimal("12.50"),
        eligible_at=NOW,
        expires_at=NOW + timedelta(days=1),
        risk_snapshot_id="b" * 64,
    ))
    ledger.reserve(order.intent_id, "backup-reservation", "50.00")
    ledger.start_send(order.intent_id, attempt_id="backup-attempt", client_order_id="backup-client")
    index.bind(BrokerOrderBinding(
        broker_order_id=41,
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        intent_id=order.intent_id,
        bt_order_ref=7,
        requested_qty=4,
        side="BUY",
        contract_id=123,
        symbol="XYZ",
        security_type="STK",
        currency="USD",
    ))
    ledger.record_broker_event(BrokerEvent(
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        intent_id=order.intent_id,
        broker_event_id="backup-fill-event",
        kind=BrokerEventKind.FILL,
        occurred_at=NOW,
        received_at=NOW,
        broker_order_id="41",
        broker_exec_id="backup-exec-1",
        quantity=2,
        price=Decimal("12.50"),
    ))
    pending_callback = BrokerEvent(
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        intent_id=order.intent_id,
        broker_event_id="backup-pending-ack",
        kind=BrokerEventKind.ACK,
        occurred_at=NOW,
        received_at=NOW,
        broker_order_id="41",
    )
    index.persist_inbox(
        "order_status:41:pending",
        {"order_id": 41, "status": "Submitted"},
        pending_callback,
    )
    ledger.set_execution_control(
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
        reason="fixture running before backup",
    )

    journal.create_run(
        run_id="backup-run",
        deployment_id="deployment-1",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        strategy_version_hash="a" * 64,
    )
    staged = journal.stage_next_event("backup-run", {"seq": 1, "close": 12.5}, event_id="backup-event")
    journal.release_next_event("backup-run")
    journal.commit_result("backup-run", WorkerEventResult(
        event_id=staged.event_id,
        state={"seen": 1},
        order_proposals=(WorkerOrderProposal(side="BUY", integer_qty=1),),
    ))
    allocator.close()
    return order.intent_id


def test_online_backup_restores_shared_execution_journal_mapping_and_pending_callback(tmp_path):
    source = tmp_path / "execution.sqlite"
    intent_id = _populated_execution_db(source)

    backup = tmp_path / "backup-001"
    live_writer = sqlite3.connect(source, isolation_level=None)
    live_writer.execute("PRAGMA journal_mode=WAL")
    live_writer.execute("CREATE TABLE backup_wal_marker(value TEXT NOT NULL)")
    live_writer.execute("INSERT INTO backup_wal_marker(value) VALUES('committed-in-wal')")
    assert (tmp_path / "execution.sqlite-wal").exists()
    manifest = create_execution_database_backup(source, backup)
    assert verify_execution_database_backup(backup) == manifest
    with sqlite3.connect(backup / "execution.sqlite3") as backup_connection:
        assert backup_connection.execute("SELECT value FROM backup_wal_marker").fetchone() == ("committed-in-wal",)
    live_writer.close()

    restored_path = tmp_path / "restored.sqlite"
    receipt = restore_execution_database_backup(backup, restored_path)
    assert receipt.requires_broker_reconciliation is True
    assert receipt.recovered_control_rows == 1

    restored_ledger = SQLiteOrderLedger(restored_path)
    restored_index = BrokerOrderIndex(restored_path)
    restored_journal = SQLiteEventJournal(restored_path)
    assert restored_ledger.get_order(intent_id).filled_qty == 2
    assert len(restored_ledger.list_fills(intent_id)) == 1
    assert restored_index.resolve_intent(intent_id).broker_order_id == 41
    assert len(restored_index.pending_inbox()) == 1
    assert restored_journal.get_checkpoint("backup-run") == {"seen": 1}
    assert len(restored_journal.list_dispatchable_outbox("backup-run", include_paused=True)) == 1
    assert restored_ledger.get_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER
    ) == (ExecutionControlState.RECOVERING, "backup_restore_requires_broker_reconciliation")


def test_restore_pauses_running_journal_until_explicit_event_recovery(tmp_path):
    source = tmp_path / "execution.sqlite"
    ledger = SQLiteOrderLedger(source)
    ledger.set_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING, reason="fixture active before backup",
    )
    journal = SQLiteEventJournal(source)
    journal.create_run(
        run_id="restore-paused-run",
        deployment_id="deployment-1",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        strategy_version_hash="a" * 64,
    )
    staged = journal.stage_next_event("restore-paused-run", {"seq": 1, "close": 12.5}, event_id="restore-event")
    released = journal.release_next_event("restore-paused-run")

    backup = tmp_path / "backup-paused-run"
    create_execution_database_backup(source, backup)
    restored_path = tmp_path / "restored-paused-run.sqlite"
    restore_execution_database_backup(backup, restored_path)
    restored = SQLiteEventJournal(restored_path)
    restored_ledger = SQLiteOrderLedger(restored_path)

    with pytest.raises(RunUnavailable, match="run is not running"):
        restored.get_run_identity("restore-paused-run")
    assert restored.get_run_identity("restore-paused-run", allow_paused=True)["deployment_id"] == "deployment-1"
    with pytest.raises(RunUnavailable, match="not running"):
        restored.stage_next_event("restore-paused-run", {"seq": 2})
    with pytest.raises(RunUnavailable, match="not running"):
        restored.release_next_event("restore-paused-run")
    with pytest.raises(RunUnavailable, match="execution scope is not running"):
        restored.resume_paused_released_event(
            "restore-paused-run",
            expected_event_id=staged.event_id,
            expected_seq=released.seq,
            expected_payload_sha256=released.payload_sha256,
        )

    # Managed recovery activates the execution gate only after reconciliation
    # and ownership checks; the journal then accepts this exact event identity.
    restored_ledger.set_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING, reason="explicit recovery completed",
    )

    resumed = restored.resume_paused_released_event(
        "restore-paused-run",
        expected_event_id=staged.event_id,
        expected_seq=released.seq,
        expected_payload_sha256=released.payload_sha256,
    )
    assert resumed == released
    assert restored.get_run_identity("restore-paused-run")["deployment_id"] == "deployment-1"


def test_backup_verification_rejects_corruption_and_schema_mismatch(tmp_path):
    source = tmp_path / "execution.sqlite"
    _populated_execution_db(source)
    backup = tmp_path / "backup"
    create_execution_database_backup(source, backup)

    manifest_path = backup / "manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    value = json.loads(original_manifest)
    value["schema_object_count"] += 1
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ExecutionDatabaseBackupError, match="schema does not match"):
        verify_execution_database_backup(backup)

    manifest_path.write_text(original_manifest, encoding="utf-8")
    database_path = backup / "execution.sqlite3"
    with database_path.open("r+b") as stream:
        stream.seek(100)
        value = stream.read(1)
        stream.seek(100)
        stream.write(bytes([value[0] ^ 0xFF]))
    with pytest.raises(ExecutionDatabaseBackupError, match="SHA-256"):
        verify_execution_database_backup(backup)


def test_restore_refuses_existing_target_without_modifying_it(tmp_path):
    source = tmp_path / "execution.sqlite"
    _populated_execution_db(source)
    backup = tmp_path / "backup"
    create_execution_database_backup(source, backup)
    target = tmp_path / "existing.sqlite"
    target.write_bytes(b"leave this file untouched")

    with pytest.raises(ExecutionDatabaseBackupError, match="already exists"):
        restore_execution_database_backup(backup, target)
    assert target.read_bytes() == b"leave this file untouched"


def test_restore_rejects_unsupported_execution_run_schema(tmp_path):
    source = tmp_path / "execution.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE execution_run(run_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO execution_run(run_id) VALUES('legacy-run')")
    backup = tmp_path / "backup-unsupported-run-schema"
    create_execution_database_backup(source, backup)

    target = tmp_path / "restored-unsupported.sqlite"
    with pytest.raises(ExecutionDatabaseBackupError, match="execution_run table has an unsupported schema"):
        restore_execution_database_backup(backup, target)
    assert not target.exists()


def test_failed_schema_migration_rolls_back_and_keeps_verified_pre_migration_backup(tmp_path):
    database = tmp_path / "execution.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing_record(value TEXT NOT NULL)")
        connection.execute("INSERT INTO existing_record(value) VALUES('kept')")
        connection.execute("PRAGMA user_version=3")

    def migration(connection):
        connection.execute("ALTER TABLE existing_record ADD COLUMN new_value TEXT")
        raise RuntimeError("simulated migration failure")

    backup = tmp_path / "pre-migration"
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        apply_atomic_execution_database_migration(database, backup, migration)

    assert verify_execution_database_backup(backup).user_version == 3
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(existing_record)")}
        assert "new_value" not in columns
        assert connection.execute("SELECT value FROM existing_record").fetchone() == ("kept",)


def test_successful_schema_migration_keeps_restorable_pre_migration_backup(tmp_path):
    database = tmp_path / "execution.sqlite"
    _populated_execution_db(database)
    with sqlite3.connect(database) as connection:
        original_user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    backup = tmp_path / "pre-migration"

    def migration(connection):
        connection.execute(
            "CREATE TABLE migration_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO migration_probe(value) VALUES('migrated')")
        connection.execute(f"PRAGMA user_version={original_user_version + 1}")

    manifest = apply_atomic_execution_database_migration(database, backup, migration)

    assert manifest == verify_execution_database_backup(backup)
    assert manifest.user_version == original_user_version
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == original_user_version + 1
        assert connection.execute("SELECT value FROM migration_probe").fetchone() == ("migrated",)

    restored_path = tmp_path / "restored.sqlite"
    receipt = restore_execution_database_backup(backup, restored_path)
    assert receipt.requires_broker_reconciliation is True
    assert receipt.recovered_control_rows == 1
    with sqlite3.connect(restored_path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == original_user_version
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
        ).fetchone() is None
    restored_ledger = SQLiteOrderLedger(restored_path)
    assert restored_ledger.get_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER
    ) == (ExecutionControlState.RECOVERING, "backup_restore_requires_broker_reconciliation")
