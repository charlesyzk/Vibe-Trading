from __future__ import annotations

import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

import pytest

from src.execution.contracts import Environment
from src.execution.execution_service import PaperExecutionService
from src.execution.ibkr_callback_pump import (
    BrokerOrderBinding,
    BrokerOrderIndex,
    IbkrCallbackPump,
    IbkrCallbackPumpWorker,
)
from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime, ManagedRuntimeError
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator
from src.execution.ibkr_reconciliation import (
    IbkrReconciliationCoordinator,
    IbkrRecoveryReconciler,
    SQLiteIbkrSnapshotStore,
)
from src.execution.ibkr_runtime_process import IbkrRuntimeHealthMonitor, IbkrRuntimeProcess
from src.execution.order_ledger import (
    BrokerEvent,
    BrokerEventKind,
    ExecutionControlState,
    OrderIntentInput,
    SQLiteOrderLedger,
)


class FakeRuntimeProcess(IbkrRuntimeProcess):
    def __init__(self, path, *, index, store, account="paper-fixture"):
        super().__init__(python=__import__("sys").executable, cwd=path.parent, spool_path=path)
        self.index = index
        self.store = store
        self.account = account
        self.connected = False
        self.closed = False
        self.start_calls = []
        self.reconciliation_calls = []
        self.cancel_calls = []
        self.cancel_error = None

    def start_process(self):
        return {"state": "NEW"}

    def request(self, command, **payload):
        if command == "start":
            self.start_calls.append(payload)
            self.connected = True
            return {"state": "STARTING"}
        if command == "status":
            return {
                "state": "RUNNING" if self.connected else "NEW",
                "socket_connected": self.connected,
                "sdk_api_ready": self.connected,
                "ready_for_dispatch": False,
                "callback_overflow": False,
                "run_error": None,
            }
        if command == "request_order_ids":
            self._queue.put(_raw("next_valid_id", order_id=1))
            return {"requested": True}
        if command == "request_reconciliation":
            self.reconciliation_calls.append(payload["snapshot_id"])
            snapshot_id = payload["snapshot_id"]
            for tag, value in (("NetLiquidation", "10000"), ("TotalCashValue", "9000"),
                               ("AvailableFunds", "8000")):
                self.store.record("account_summary", {
                    "snapshot_id": snapshot_id, "request_id": snapshot_id,
                    "account": self.account, "tag": tag, "value": value, "currency": "USD",
                })
            self.store.record("account_summary_end", {"snapshot_id": snapshot_id, "request_id": snapshot_id})
            self.store.record("positions_end", {"snapshot_id": snapshot_id})
            self.store.record("open_orders_end", {"snapshot_id": snapshot_id})
            return {"snapshot_id": snapshot_id}
        raise AssertionError(command)

    def close(self, *, timeout=3.0):
        self.closed = True
        self.connected = False

    def cancel_order(self, broker_order_id):
        self.cancel_calls.append(broker_order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"broker_order_id": broker_order_id}


def _raw(kind, **payload):
    return SimpleNamespace(kind=kind, payload=tuple(sorted(payload.items())))


def _build_runtime(tmp_path, *, managed_account="paper-fixture", gateway_port=4002):
    path = tmp_path / "runtime.sqlite"
    ledger = SQLiteOrderLedger(path)
    index = BrokerOrderIndex(path)
    allocator = IbkrOrderIdAllocator(path)
    store = SQLiteIbkrSnapshotStore(path)
    events = queue.Queue()
    overflowed = threading.Event()
    process = FakeRuntimeProcess(path, index=index, store=store, account=managed_account)
    pump = IbkrCallbackPump(
        events=events,
        overflowed=overflowed,
        ledger=ledger,
        order_index=index,
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        callback_sink=lambda _callback: None,
        next_valid_id_sink=allocator.observe_next_valid_id,
        reconciliation_sink=store.record,
    )
    process._queue = events
    service = PaperExecutionService(
        ledger=ledger,
        lock_directory=tmp_path / "locks",
        broker="IBKR",
        account_ref="paper-fixture",
        environment=Environment.PAPER,
        owner_id="runtime-test",
        recovery_check=lambda: True,
        activation_check=lambda: True,
        quiesce_check=lambda: True,
    )
    worker = IbkrCallbackPumpWorker(pump, poll_interval=0.001)
    health = IbkrRuntimeHealthMonitor.for_process(process, pause=lambda reason: service.pause(reason=reason))
    coordinator = IbkrReconciliationCoordinator(store=store, process=process, account_ref="paper-fixture")
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator, account_ref="paper-fixture",
    )
    runtime = IbkrManagedPaperRuntime(
        service=service,
        process=process,
        callback_pump=pump,
        callback_worker=worker,
        health_monitor=health,
        coordinator=coordinator,
        reconciler=reconciler,
        ledger=ledger,
        order_index=index,
        allocator=allocator,
        snapshot_store=store,
        gateway_host="127.0.0.1",
        gateway_port=gateway_port,
        client_id=7,
        account_ref="paper-fixture",
        recovery_timeout=1.0,
        snapshot_timeout=1.0,
        stop_timeout=1.0,
        poll_interval=0.001,
        snapshot_id_factory=lambda: 1900000001,
    )
    return runtime, process, service, allocator, events


def test_runtime_construction_is_offline_then_reconciles_to_paused_before_activation(tmp_path):
    runtime, process, service, allocator, events = _build_runtime(tmp_path)
    assert process.start_calls == []
    assert not process.connected
    with pytest.raises(ManagedRuntimeError, match="only while runtime is PAUSED"):
        runtime.latest_reconciled_snapshot()

    def queue_handshake(payload):
        events.put(_raw("next_valid_id", order_id=1))
        events.put(_raw("managed_accounts", accounts=payload["account_ref"]))

    # The fake SDK callback source is kept separate from construction; emulate
    # its two handshake callbacks as soon as the explicit start command occurs.
    original_request = process.request

    def request(command, **payload):
        result = original_request(command, **payload)
        if command == "start":
            queue_handshake({"account_ref": "paper-fixture"})
        return result

    process.request = request
    runtime.start_paused()
    assert process.start_calls == [{"host": "127.0.0.1", "port": 4002, "client_id": 7}]
    assert service.state()[0] is ExecutionControlState.PAUSED
    assert allocator.allocation_ready is True
    snapshot = runtime.latest_reconciled_snapshot()
    assert snapshot["capture_complete"] is True
    assert snapshot["reconciled"] is True
    assert snapshot["account_ref"] == "paper-fixture"
    assert snapshot["environment"] == "paper"
    assert snapshot["reconciliation_result"]["checks"] == [
        "scope", "open_orders", "positions", "order_id_high_water_mark"
    ]
    old_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    with sqlite3.connect(runtime.snapshot_store.path) as db:
        db.execute(
            "UPDATE ibkr_reconciliation_run SET completed_at_utc=? WHERE snapshot_id=?",
            (old_timestamp, snapshot["snapshot_id"]),
        )
    with pytest.raises(ManagedRuntimeError, match="freshness window"):
        runtime.latest_reconciled_snapshot()
    with sqlite3.connect(runtime.snapshot_store.path) as db:
        db.execute(
            "UPDATE ibkr_reconciliation_run SET completed_at_utc=? WHERE snapshot_id=?",
            (datetime.now(timezone.utc).isoformat(), snapshot["snapshot_id"]),
        )
    runtime.latest_reconciled_snapshot()

    runtime.activate()
    assert service.state()[0] is ExecutionControlState.RUNNING
    with pytest.raises(ManagedRuntimeError, match="only while runtime is PAUSED"):
        runtime.latest_reconciled_snapshot()
    runtime.pause(reason="test pause")
    assert service.state()[0] is ExecutionControlState.PAUSED
    with pytest.raises(ManagedRuntimeError, match="only while runtime is PAUSED"):
        runtime.latest_reconciled_snapshot()
    assert runtime.stop() is True
    assert process.closed is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    assert service.lease_held is False
    with pytest.raises(ManagedRuntimeError, match="only while runtime is PAUSED"):
        runtime.latest_reconciled_snapshot()
    allocator.close()


def test_same_connected_runtime_recovery_requests_fresh_next_valid_id(tmp_path):
    runtime, process, service, allocator, events = _build_runtime(tmp_path)
    _start_runtime(runtime, process, events)
    runtime.snapshot_id_factory = lambda: 1900000002

    runtime.start_paused()

    assert process.reconciliation_calls == [1900000001, 1900000002]
    assert service.state()[0] is ExecutionControlState.PAUSED
    assert allocator.allocation_ready is True
    assert runtime._latest_snapshot_id == 1900000002
    assert runtime.stop() is True
    allocator.close()


@pytest.mark.parametrize("port", [4001, 4003, 7496, 7498])
def test_runtime_constructor_rejects_live_and_unprofiled_ports(tmp_path, port):
    with pytest.raises(ValueError, match="only default IB Gateway/TWS PAPER ports"):
        _build_runtime(tmp_path, gateway_port=port)


def _authorized_active_order(runtime, *, account="paper-fixture"):
    now = datetime.now(timezone.utc)
    order = runtime.ledger.create_intent(OrderIntentInput(
        decision_id="manager-cancel-decision",
        broker="IBKR",
        account_ref=account,
        environment=Environment.PAPER,
        deployment_id="deployment-1",
        strategy_version_hash="a" * 64,
        input_event_seq=1,
        action_index=0,
        bar_end=now,
        instrument_id="XYZ",
        side="BUY",
        integer_qty=2,
        reference_price="12.50",
        eligible_at=now,
        expires_at=now + timedelta(days=1),
        risk_snapshot_id="b" * 64,
    ))
    runtime.ledger.reserve(order.intent_id, "manager-cancel-reservation", "25.00")
    runtime.ledger.start_send(
        order.intent_id, attempt_id="manager-cancel-attempt", client_order_id="manager-cancel-client"
    )
    runtime.callback_pump.bind_order(BrokerOrderBinding(
        broker_order_id=41,
        account_ref=account,
        environment=Environment.PAPER,
        intent_id=order.intent_id,
        bt_order_ref=7,
        requested_qty=2,
        side="BUY",
        contract_id=123,
        symbol="XYZ",
        security_type="STK",
        currency="USD",
    ))
    runtime.ledger.record_broker_event(BrokerEvent(
        broker="IBKR",
        account_ref=account,
        environment=Environment.PAPER,
        intent_id=order.intent_id,
        broker_event_id="manager-cancel-ack",
        kind=BrokerEventKind.ACK,
        occurred_at=now,
        received_at=now,
        broker_order_id="41",
    ))
    return order


def _start_runtime(runtime, process, events):
    original_request = process.request

    def request(command, **payload):
        result = original_request(command, **payload)
        if command == "start":
            events.put(_raw("next_valid_id", order_id=1))
            events.put(_raw("managed_accounts", accounts="paper-fixture"))
        return result

    process.request = request
    runtime.start_paused()


def test_managed_runtime_manager_cancel_is_bound_and_confirmation_is_callback_driven(tmp_path):
    runtime, process, _service, allocator, events = _build_runtime(tmp_path)
    _start_runtime(runtime, process, events)
    order = _authorized_active_order(runtime)

    pending = runtime.cancel_owned_order(order.intent_id)

    assert pending.state.value == "CANCEL_PENDING"
    assert process.cancel_calls == [41]
    with pytest.raises(ManagedRuntimeError, match="already attempted"):
        runtime.cancel_owned_order(order.intent_id)
    assert process.cancel_calls == [41]

    events.put(_raw("order_status", order_id=41, status="Cancelled"))
    deadline = time.monotonic() + 1.0
    while runtime.ledger.get_order(order.intent_id).state.value != "CANCELLED" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime.ledger.get_order(order.intent_id).state.value == "CANCELLED"
    with sqlite3.connect(runtime.ledger.path) as db:
        assert db.execute("SELECT state FROM cancel_attempt WHERE intent_id=?", (order.intent_id,)).fetchone()[0] == "CONFIRMED"
    assert runtime.stop() is True
    allocator.close()


def test_managed_runtime_cancel_send_unknown_is_frozen_without_retry(tmp_path):
    runtime, process, _service, allocator, events = _build_runtime(tmp_path)
    _start_runtime(runtime, process, events)
    order = _authorized_active_order(runtime)
    process.cancel_error = TimeoutError("IPC reply lost after dispatch")

    with pytest.raises(ManagedRuntimeError, match="unknown; reconcile before retry"):
        runtime.cancel_owned_order(order.intent_id)
    assert process.cancel_calls == [41]
    with sqlite3.connect(runtime.ledger.path) as db:
        assert db.execute("SELECT state FROM cancel_attempt WHERE intent_id=?", (order.intent_id,)).fetchone()[0] == "UNKNOWN"
    with pytest.raises(ManagedRuntimeError, match="already attempted"):
        runtime.cancel_owned_order(order.intent_id)
    assert process.cancel_calls == [41]
    # A broker callback can still settle an outcome-unknown request. The test
    # confirms it before teardown so the worker and lifecycle lease drain.
    events.put(_raw("order_status", order_id=41, status="Cancelled"))
    deadline = time.monotonic() + 1.0
    while runtime.ledger.get_order(order.intent_id).state.value != "CANCELLED" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime.ledger.get_order(order.intent_id).state.value == "CANCELLED"
    assert runtime.stop() is True
    allocator.close()


def test_runtime_rejects_wrong_managed_account_and_keeps_dispatch_closed(tmp_path):
    runtime, process, service, allocator, events = _build_runtime(tmp_path, managed_account="other-account")
    original_request = process.request

    def request(command, **payload):
        result = original_request(command, **payload)
        if command == "start":
            events.put(_raw("next_valid_id", order_id=1))
            events.put(_raw("managed_accounts", accounts="other-account"))
        return result

    process.request = request
    with pytest.raises(ManagedRuntimeError, match="callback consumer failed"):
        runtime.start_paused()
    assert service.state()[0] is ExecutionControlState.PAUSED
    assert service.lease_held is True
    assert allocator.allocation_ready is False
    # The failed pump means safe shutdown cannot be proved. Keep the scope
    # lease and STOPPING state instead of pretending cleanup succeeded.
    assert runtime.stop() is False
    assert service.lease_held is True
    allocator.close()


def test_runtime_rejects_ambiguous_account_set_before_snapshot_or_recovery(tmp_path):
    runtime, process, service, allocator, events = _build_runtime(tmp_path)
    original_request = process.request

    def request(command, **payload):
        result = original_request(command, **payload)
        if command == "start":
            events.put(_raw("next_valid_id", order_id=1))
            events.put(_raw("managed_accounts", accounts="paper-fixture,other-account"))
        return result

    process.request = request
    with pytest.raises(ManagedRuntimeError, match="callback consumer failed"):
        runtime.start_paused()

    assert process.reconciliation_calls == []
    assert service.state()[0] is ExecutionControlState.PAUSED
    assert service.lease_held is True
    assert allocator.allocation_ready is False
    assert not runtime._connected
    assert runtime.stop() is False
    allocator.close()
