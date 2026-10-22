from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.execution.ibkr_reconciliation import (
    IbkrRecoveryReconciler,
    IbkrReconciliationCoordinator,
    ReconciliationSnapshotError,
    SQLiteIbkrSnapshotStore,
)
from src.execution.ibkr_callback_pump import BrokerOrderIndex
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator, OrderIdNotReady
from src.execution.order_ledger import SQLiteOrderLedger


SNAPSHOT = 1900000001


def _account(tag: str, value: str):
    return {
        "snapshot_id": SNAPSHOT,
        "request_id": SNAPSHOT,
        "account": "paper-fixture",
        "tag": tag,
        "value": value,
        "currency": "USD",
    }


def _complete(store: SQLiteIbkrSnapshotStore):
    for tag, value in (
        ("NetLiquidation", "10000.00"),
        ("TotalCashValue", "9000.00"),
        ("AvailableFunds", "8000.00"),
    ):
        store.record("account_summary", _account(tag, value))
    store.record("position", {
        "snapshot_id": SNAPSHOT, "account": "paper-fixture", "contract_id": 265598,
        "symbol": "AAPL", "currency": "USD", "quantity": "2", "average_cost": "100.5",
    })
    store.record("open_order", {
        "snapshot_id": SNAPSHOT, "account": "paper-fixture", "order_id": 41,
        "permanent_id": 0, "contract_id": 265598, "symbol": "AAPL",
        "security_type": "STK", "exchange": "SMART", "primary_exchange": "NASDAQ",
        "currency": "USD", "action": "BUY", "quantity": "1",
        "order_type": "MKT", "time_in_force": "DAY", "outside_rth": False,
        "status": "Submitted", "warning": "",
    })
    store.record("account_summary_end", {"snapshot_id": SNAPSHOT, "request_id": SNAPSHOT})
    store.record("positions_end", {"snapshot_id": SNAPSHOT})
    store.record("open_orders_end", {"snapshot_id": SNAPSHOT})


def _empty_account_snapshot(store: SQLiteIbkrSnapshotStore, snapshot_id: int):
    for tag, value in (
        ("NetLiquidation", "10000"),
        ("TotalCashValue", "9000"),
        ("AvailableFunds", "8000"),
    ):
        store.record("account_summary", {
            "snapshot_id": snapshot_id, "request_id": snapshot_id,
            "account": "paper-fixture", "tag": tag, "value": value, "currency": "USD",
        })
    store.record("account_summary_end", {"snapshot_id": snapshot_id, "request_id": snapshot_id})
    store.record("positions_end", {"snapshot_id": snapshot_id})
    store.record("open_orders_end", {"snapshot_id": snapshot_id})


def test_snapshot_capture_requires_all_end_markers_and_required_account_facts(tmp_path):
    store = SQLiteIbkrSnapshotStore(tmp_path / "runtime.sqlite")
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    with pytest.raises(ReconciliationSnapshotError, match="end markers"):
        store.complete_snapshot(SNAPSHOT)

    _complete(store)
    result = store.complete_snapshot(SNAPSHOT)
    assert result["capture_complete"] is True
    assert result["reconciled"] is False
    assert result["account_ref"] == "paper-fixture"
    assert len(result["positions"]) == 1
    assert len(result["open_orders"]) == 1


def test_snapshot_rows_are_idempotent_but_conflicting_duplicates_fail(tmp_path):
    store = SQLiteIbkrSnapshotStore(tmp_path / "runtime.sqlite")
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    row = _account("NetLiquidation", "10000")
    store.record("account_summary", row)
    store.record("account_summary", row)
    with pytest.raises(ReconciliationSnapshotError, match="different content"):
        store.record("account_summary", {**row, "value": "10001"})


def test_snapshot_store_migrates_legacy_run_table_without_dynamic_default(tmp_path):
    import sqlite3

    path = tmp_path / "runtime.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE ibkr_reconciliation_run ("
            "snapshot_id INTEGER PRIMARY KEY, account_ref TEXT NOT NULL, "
            "environment TEXT NOT NULL, account_summary_complete INTEGER NOT NULL DEFAULT 0, "
            "positions_complete INTEGER NOT NULL DEFAULT 0, open_orders_complete INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO ibkr_reconciliation_run(snapshot_id,account_ref,environment) "
            "VALUES(1,'paper-fixture','paper')"
        )

    store = SQLiteIbkrSnapshotStore(path)
    with sqlite3.connect(path) as db:
        requested_at = db.execute(
            "SELECT requested_at_utc FROM ibkr_reconciliation_run WHERE snapshot_id=1"
        ).fetchone()[0]
    assert requested_at
    store.begin(2, account_ref="paper-fixture")


def test_snapshot_fails_closed_on_wrong_request_id_currency_or_fractional_position(tmp_path):
    store = SQLiteIbkrSnapshotStore(tmp_path / "runtime.sqlite")
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    with pytest.raises(ReconciliationSnapshotError, match="request id"):
        store.record("account_summary", {**_account("NetLiquidation", "1"), "request_id": 99})
    with pytest.raises(ReconciliationSnapshotError, match="currency"):
        store.record("account_summary", {**_account("NetLiquidation", "1"), "currency": "CAD"})
    with pytest.raises(ReconciliationSnapshotError, match="whole share"):
        store.record("position", {
            "snapshot_id": SNAPSHOT, "account": "paper-fixture", "contract_id": 1,
            "symbol": "AAPL", "currency": "USD", "quantity": "0.5", "average_cost": "1",
        })


def test_coordinator_persists_snapshot_before_request_and_leaves_it_incomplete_on_ipc_failure(tmp_path):
    store = SQLiteIbkrSnapshotStore(tmp_path / "runtime.sqlite")

    class Process:
        def __init__(self):
            self.called = False

        def request_reconciliation(self, snapshot_id):
            assert store.path
            self.called = True
            raise TimeoutError("IPC uncertain")

    process = Process()
    coordinator = IbkrReconciliationCoordinator(
        store=store, process=process, account_ref="paper-fixture",
    )
    with pytest.raises(TimeoutError):
        coordinator.request_snapshot(SNAPSHOT)
    assert process.called
    with pytest.raises(ReconciliationSnapshotError, match="end markers"):
        coordinator.completed_capture(SNAPSHOT)


def test_recovery_reconciler_requires_empty_account_to_match_ledger_and_confirms_exact_cursor(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    _empty_account_snapshot(store, SNAPSHOT)
    allocator.observe_next_valid_id(1)

    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )
    result = reconciler.reconcile(SNAPSHOT)
    assert result["reconciled"] is True
    assert result["reconciliation_result"]["candidate_order_id"] == 1
    assert allocator.allocation_ready is True
    assert allocator.allocate() == 1
    assert store.complete_snapshot(SNAPSHOT)["reconciled"] is True
    allocator.close()


def test_snapshot_fact_tampering_before_reconciliation_keeps_allocator_closed(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    _empty_account_snapshot(store, SNAPSHOT)
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT fact_key,payload_json FROM ibkr_reconciliation_fact "
            "WHERE snapshot_id=? AND kind='account_summary' ORDER BY fact_key LIMIT 1", (SNAPSHOT,),
        ).fetchone()
        payload = json.loads(row[1])
        payload["value"] = "999999"
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        db.execute(
            "UPDATE ibkr_reconciliation_fact SET payload_json=? "
            "WHERE snapshot_id=? AND kind='account_summary' AND fact_key=?", (changed, SNAPSHOT, row[0]),
        )

    allocator.observe_next_valid_id(1)
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )
    with pytest.raises(ReconciliationSnapshotError, match="payload hash"):
        reconciler.reconcile(SNAPSHOT)
    assert allocator.allocation_ready is False
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.close()


def test_reconciled_snapshot_proof_binds_the_complete_fact_set(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    _empty_account_snapshot(store, SNAPSHOT)
    allocator.observe_next_valid_id(1)
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )
    reconciler.reconcile(SNAPSHOT)

    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT kind,fact_key,payload_json FROM ibkr_reconciliation_fact "
            "WHERE snapshot_id=? AND kind='account_summary' ORDER BY fact_key LIMIT 1", (SNAPSHOT,),
        ).fetchone()
        payload = json.loads(row[2])
        payload["value"] = "10001"
        changed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(changed.encode("utf-8")).hexdigest()
        db.execute(
            "UPDATE ibkr_reconciliation_fact SET payload_json=?,payload_sha256=? "
            "WHERE snapshot_id=? AND kind=? AND fact_key=?",
            (changed, digest, SNAPSHOT, row[0], row[1]),
        )

    with pytest.raises(ReconciliationSnapshotError, match="proof does not match"):
        store.complete_snapshot(SNAPSHOT)
    allocator.close()


def test_recovery_position_mismatch_does_not_confirm_order_id_candidate(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    for tag, value in (("NetLiquidation", "10000"), ("TotalCashValue", "9000"),
                       ("AvailableFunds", "8000")):
        store.record("account_summary", {
            "snapshot_id": SNAPSHOT, "request_id": SNAPSHOT, "account": "paper-fixture",
            "tag": tag, "value": value, "currency": "USD",
        })
    store.record("position", {
        "snapshot_id": SNAPSHOT, "account": "paper-fixture", "contract_id": 1,
        "symbol": "AAPL", "currency": "USD", "quantity": "1", "average_cost": "100",
    })
    store.record("account_summary_end", {"snapshot_id": SNAPSHOT, "request_id": SNAPSHOT})
    store.record("positions_end", {"snapshot_id": SNAPSHOT})
    store.record("open_orders_end", {"snapshot_id": SNAPSHOT})
    allocator.observe_next_valid_id(1)
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )
    with pytest.raises(ReconciliationSnapshotError, match="positions differ"):
        reconciler.reconcile(SNAPSHOT)
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.close()


def test_recovery_rejects_broker_open_order_without_local_binding(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    _complete(store)  # Broker reports order 41, but the local binding table is empty.
    allocator.observe_next_valid_id(42)
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )

    with pytest.raises(ReconciliationSnapshotError, match="not mapped"):
        reconciler.reconcile(SNAPSHOT)
    assert allocator.allocation_ready is False
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.close()


def test_recovery_reconciler_binds_confirmation_to_the_candidate_it_checked(tmp_path):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    store.begin(SNAPSHOT, account_ref="paper-fixture")
    _empty_account_snapshot(store, SNAPSHOT)
    allocator.observe_next_valid_id(1)
    original_mark = store.mark_reconciled

    def mutate_candidate(snapshot_id, result):
        allocator.observe_next_valid_id(2)
        original_mark(snapshot_id, result)

    store.mark_reconciled = mutate_candidate
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator,
        account_ref="paper-fixture",
    )
    with pytest.raises(OrderIdNotReady, match="candidate changed"):
        reconciler.reconcile(SNAPSHOT)
    assert allocator.allocation_ready is False
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.close()
