from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import pwd
import queue
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest
import backtrader as bt

from src.bt_runtime.brokers.proxy import BrokerCallback, CallbackStatus
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.contracts import Bar, Environment, RiskGrant, SourceKind, StrategySpec
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.event_journal import RunUnavailable, SQLiteEventJournal
from src.execution.event_worker import EventWorkerError, PythonEventCoordinator
from src.execution.execution_service import ExecutionServiceNotReady, PaperExecutionService
from src.execution.ibkr_callback_pump import (
    BrokerOrderBinding, BrokerOrderIndex, IbkrCallbackPump, IbkrCallbackPumpWorker,
)
from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator
from src.execution.ibkr_reconciliation import (
    IbkrReconciliationCoordinator,
    IbkrRecoveryReconciler,
    SQLiteIbkrSnapshotStore,
)
from src.execution.ibkr_runtime_process import (
    IbkrRuntimeHealthMonitor, IbkrRuntimeProcess, IbkrSubprocessCancelTransport,
    IbkrSubprocessPaperTransport,
)
from src.execution.managed_python_paper_session import (
    CallbackSinkRouter,
    ManagedPythonPaperSession,
    ManagedPythonPaperSessionError,
)
from src.execution.python_sandbox import PythonSandbox, SandboxLimits, SandboxTimeout
from src.execution.order_ledger import (
    BrokerEventKind, ExecutionControlState, LocalOrderState, OrderIntentInput, SQLiteOrderLedger,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.outbox_dispatch import (
    AuthorizedProposal, ProposalRiskInputs, PythonOutboxIntentHandoff,
    RiskAwareProposalAuthorizer,
)
from src.execution.paper_execution_manager import (
    PaperExecutionManager, PaperRuntimeAuthorizationError, PaperRuntimeScope,
)
from src.execution.risk import PositionFact, RiskDecision, RiskFacts
from src.execution.versions import StrategyVersionStore
from src.execution.trusted_template_paper import TargetFractionPaperSizer, TrustedTemplateEventCoordinator
from src.execution.backtrader_dispatch import ContractIdentity
from src.session.models import AuthMethod, Principal


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def test_callback_router_stages_in_order_and_fails_closed_at_capacity():
    router = CallbackSinkRouter(capacity=2)
    callbacks = [BrokerCallback(
        order_ref=1, status=CallbackStatus.ACCEPTED, event_id=f"event-{index}", occurred_at=NOW,
    ) for index in range(3)]
    router(callbacks[0])
    router(callbacks[1])
    with pytest.raises(ManagedPythonPaperSessionError, match="staging queue is full"):
        router(callbacks[2])
    observed = []
    router.attach(observed.append)
    assert observed == callbacks[:2]
    assert router.pending_count == 0


class NoProposalSandbox:
    def __init__(self, proposals=()):
        self.proposals = list(proposals)

    def run_json(self, request):
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"events": 1},
            "order_proposals": self.proposals,
        })


class StatefulCheckpointSandbox:
    """Deterministic worker that requires persisted state to resume correctly."""

    def __init__(self, proposals=(), proposal_event_ids=None):
        self.requests = []
        self.proposals = list(proposals)
        self.proposal_event_ids = None if proposal_event_ids is None else set(proposal_event_ids)

    def run_json(self, request):
        self.requests.append(request)
        previous = request["state"]
        close = Decimal(request["event"]["bar"]["close"])
        state = {
            "event_count": int(previous.get("event_count", 0)) + 1,
            "close_total": str(Decimal(previous.get("close_total", "0")) + close),
            "last_close": str(close),
        }
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": state,
            "order_proposals": (
                self.proposals
                if self.proposal_event_ids is None or request["event_id"] in self.proposal_event_ids
                else []
            ),
        })


class FailOnceSandbox:
    def __init__(self):
        self.calls = []

    def run_json(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            raise TimeoutError("injected strategy worker timeout")
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"recovered": True},
            "order_proposals": [],
        })


class BlockingFirstEventSandbox:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run_json(self, request):
        self.calls += 1
        proposals = []
        if self.calls == 1:
            proposals = [{"side": "BUY", "integer_qty": 2, "reason": "entry"}]
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test strategy remained blocked past its deadline")
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"events": self.calls},
            "order_proposals": proposals,
        })


class FakeRuntimeProcess(IbkrRuntimeProcess):
    def __init__(self, path, *, store, callbacks):
        super().__init__(python=__import__("sys").executable, cwd=path.parent, spool_path=path)
        self.store = store
        self.callbacks = callbacks
        self.connected = False
        self.closed = False
        self.order_sends = []
        self.cancel_calls = []
        self.auto_ack_on_send = False
        self.auto_fill_on_send = True
        self.next_valid_id = 1
        self.snapshot_positions = []
        self.snapshot_open_orders = []

    def start_process(self):
        return {"state": "NEW"}

    def request(self, command, **payload):
        if command == "start":
            self.connected = True
            self.callbacks.put(SimpleNamespace(
                kind="next_valid_id", payload=(("order_id", self.next_valid_id),),
            ))
            self.callbacks.put(SimpleNamespace(
                kind="managed_accounts", payload=(("accounts", "paper-session"),),
            ))
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
            self.next_valid_id += 1
            self.callbacks.put(SimpleNamespace(
                kind="next_valid_id", payload=(("order_id", self.next_valid_id),),
            ))
            return {"requested": True}
        if command == "request_reconciliation":
            snapshot_id = payload["snapshot_id"]
            for tag, value in (("NetLiquidation", "10000"), ("TotalCashValue", "9000"),
                               ("AvailableFunds", "8000")):
                self.store.record("account_summary", {
                    "snapshot_id": snapshot_id, "request_id": snapshot_id,
                    "account": "paper-session", "tag": tag, "value": value, "currency": "USD",
                })
            self.store.record("account_summary_end", {"snapshot_id": snapshot_id, "request_id": snapshot_id})
            for position in self.snapshot_positions:
                self.store.record("position", {"snapshot_id": snapshot_id, **position})
            self.store.record("positions_end", {"snapshot_id": snapshot_id})
            for order in self.snapshot_open_orders:
                self.store.record("open_order", {"snapshot_id": snapshot_id, **order})
            self.store.record("open_orders_end", {"snapshot_id": snapshot_id})
            return {"snapshot_id": snapshot_id}
        if command == "place_order":
            self.order_sends.append(payload)
            order = payload["order"]
            contract = payload["contract"]
            if self.auto_ack_on_send:
                self.callbacks.put(SimpleNamespace(kind="order_status", payload=tuple(sorted({
                    "request_id": 1, "order_id": payload["broker_order_id"],
                    "status": "Submitted", "filled": "0", "remaining": str(order["quantity"]),
                }.items()))))
            if self.auto_fill_on_send:
                self.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
                    "request_id": 1, "execution_id": f"managed-exec-{payload['broker_order_id']}",
                    "order_id": payload["broker_order_id"], "account": order["account_ref"],
                    "side": "BOT" if order["action"] == "BUY" else "SLD",
                    "shares": str(order["quantity"]), "price": "101.0",
                    "time": "20261021  06:00:00", "symbol": contract["symbol"],
                    "contract_id": contract["con_id"], "security_type": contract["security_type"],
                    "currency": contract["currency"],
                }.items()))))
            return {"accepted": True}
        if command == "cancel_order":
            self.cancel_calls.append(payload["broker_order_id"])
            return {"accepted": True}
        raise AssertionError(command)

    def close(self, *, timeout=3.0):
        self.closed = True
        self.connected = False


def _spec():
    return StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "managed-session-test",
        "source_kind": SourceKind.PYTHON_UPLOAD.value, "source_artifact_id": "upload-1",
        "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
        "parameters": {},
        "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
    })


def _make_runtime(tmp_path, database):
    ledger = SQLiteOrderLedger(database)
    index = BrokerOrderIndex(database)
    allocator = IbkrOrderIdAllocator(database)
    store = SQLiteIbkrSnapshotStore(database)
    callbacks = queue.Queue()
    process = FakeRuntimeProcess(database, store=store, callbacks=callbacks)
    pump = IbkrCallbackPump(
        events=callbacks, overflowed=threading.Event(), ledger=ledger, order_index=index,
        broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
        callback_sink=lambda _callback: None, next_valid_id_sink=allocator.observe_next_valid_id,
        reconciliation_sink=store.record,
    )
    service = PaperExecutionService(
        ledger=ledger, lock_directory=tmp_path / "locks", broker="IBKR",
        account_ref="paper-session", environment=Environment.PAPER,
        owner_id="managed-session-test", recovery_check=lambda: True,
        activation_check=lambda: True, quiesce_check=lambda: True,
    )
    worker = IbkrCallbackPumpWorker(pump, poll_interval=0.001)
    health = IbkrRuntimeHealthMonitor.for_process(process, pause=lambda reason: service.pause(reason=reason))
    coordinator = IbkrReconciliationCoordinator(store=store, process=process, account_ref="paper-session")
    reconciler = IbkrRecoveryReconciler(
        store=store, ledger=ledger, order_index=index, allocator=allocator, account_ref="paper-session",
    )
    runtime = IbkrManagedPaperRuntime(
        service=service, process=process, callback_pump=pump, callback_worker=worker,
        health_monitor=health, coordinator=coordinator, reconciler=reconciler,
        ledger=ledger, order_index=index, allocator=allocator, snapshot_store=store,
        gateway_host="127.0.0.1", gateway_port=4002, client_id=44,
        account_ref="paper-session", recovery_timeout=1, snapshot_timeout=1,
        stop_timeout=1, poll_interval=0.001, snapshot_id_factory=lambda: 1900000001,
    )
    runtime.paper_transport = lambda *args, **kwargs: pytest.fail("no proposal should reach transport")
    runtime.cancel_transport = lambda *_args: pytest.fail("no cancellation expected")
    return runtime, service, ledger, index, allocator


def test_managed_session_uses_fresh_snapshot_and_real_cerebro_while_paused(tmp_path):
    database = tmp_path / "managed.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-1", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("no order requires a risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("dispatch port must own sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-1", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    snapshot = session.start_paused()
    assert snapshot["reconciled"] is True
    assert session.broker is not None and session.broker.getcash() == 9000
    assert service.state()[0] is ExecutionControlState.PAUSED
    with pytest.raises(ExecutionServiceNotReady, match="only while explicitly RUNNING"):
        session.push_completed_bar(Bar(
            instrument_id="AAPL", session_date=date(2026, 10, 20),
            start_at=NOW.replace(day=20, hour=13, minute=30),
            end_at=NOW.replace(day=20, hour=20), available_at=NOW.replace(day=20, hour=20),
            timezone="America/New_York", open=Decimal("100"), high=Decimal("100"),
            low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"),
            currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
        ))

    runtime.activate()
    session.push_completed_bar(Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=NOW.replace(day=20, hour=13, minute=30),
        end_at=NOW.replace(day=20, hour=20), available_at=NOW.replace(day=20, hour=20),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    ))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            cursor = db.execute("SELECT next_seq FROM execution_run WHERE run_id='run-1'").fetchone()
        if cursor and cursor[0] == 2:
            break
        time.sleep(0.005)
    assert cursor == (2,)
    assert session.stop(timeout=2) is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    allocator.close()


def test_authorized_recovery_rebuilds_cerebro_and_retries_exact_event_after_separate_activate(tmp_path):
    database = tmp_path / "managed-session-recovery.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-session-recovery", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    sandbox = FailOnceSandbox()
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("no order requires a risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("recovery must not use a fallback sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc),
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-session-recovery", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01,
        now=lambda: datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc),
    )
    scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
    manager = PaperExecutionManager(
        runtime_factory=lambda _scope: session,
        control_authorizer=lambda principal, requested_scope, action: (
            principal.attributable and requested_scope == scope and action in {"activate", "recover", "stop"}
        ),
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    principal = Principal(subject="recovery-human", auth_method=AuthMethod.FEDERATED_IDENTITY)
    bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=NOW.replace(day=20, hour=13, minute=30),
        end_at=NOW.replace(day=20, hour=20), available_at=NOW.replace(day=20, hour=20),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("102"),
        low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="recovery-fixture", adjustment="unadjusted",
    )
    try:
        manager.start(scope)
        manager.activate(scope, principal)
        session.push_completed_bar(bar)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            thread = session._thread
            if session.exception is not None and thread is not None and not thread.is_alive():
                break
            time.sleep(0.005)
        assert session.exception is not None
        assert session._thread is not None and not session._thread.is_alive()
        failure: BaseException | None = session.exception
        while failure is not None and not isinstance(failure, EventWorkerError):
            failure = failure.__cause__
        assert isinstance(failure, EventWorkerError)
        assert service.state()[0] is ExecutionControlState.PAUSED
        failed_event = journal.inspect_paused_released_event("run-session-recovery")
        assert failed_event.payload == {"bar": bar.model_dump(mode="json")}
        assert failed_event.seq == 1
        assert runtime.process.order_sends == []

        runtime.snapshot_id_factory = lambda: 1900000002
        recovered = manager.recover_failed_session(scope, principal)
        assert recovered.control_state is ExecutionControlState.PAUSED
        assert recovered.owner_lease_held is True
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert session.health_error_code is None
        assert len(sandbox.calls) == 1, "recovery must not invoke the strategy worker before activation"
        assert journal.inspect_paused_released_event("run-session-recovery") == failed_event
        assert runtime.process.order_sends == []

        manager.activate(scope, principal)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with sqlite3.connect(database) as db:
                row = db.execute(
                    "SELECT state,next_seq FROM execution_run WHERE run_id='run-session-recovery'"
                ).fetchone()
                result = db.execute(
                    "SELECT event_id FROM event_result WHERE run_id='run-session-recovery' AND seq=1"
                ).fetchone()
            if row == ("RUNNING", 2) and result is not None:
                break
            time.sleep(0.005)
        assert row == ("RUNNING", 2)
        assert result == (failed_event.event_id,)
        assert len(sandbox.calls) == 2
        assert sandbox.calls[0]["event_id"] == sandbox.calls[1]["event_id"] == failed_event.event_id
        assert sandbox.calls[0]["event"] == sandbox.calls[1]["event"]
        assert runtime.process.order_sends == []
        assert manager.stop(scope, principal) is True
        assert service.state()[0] is ExecutionControlState.STOPPED
    finally:
        if service.lease_held:
            runtime.stop()
        allocator.close()


def test_failed_session_recovery_replays_unacked_partial_buy_without_duplicate_fill_or_send(tmp_path):
    database = tmp_path / "managed-session-recovery-active-buy.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    runtime.process.auto_fill_on_send = False
    runtime.process.next_valid_id = 6
    runtime.process.snapshot_positions = [{
        "request_id": 1900000001, "account": "paper-session", "contract_id": 123,
        "symbol": "AAPL", "currency": "USD", "quantity": "2", "average_cost": "102",
    }]
    runtime.process.snapshot_open_orders = [{
        "order_id": 5, "account": "paper-session", "permanent_id": 50, "contract_id": 123,
        "symbol": "AAPL", "security_type": "STK", "exchange": "SMART",
        "primary_exchange": "NASDAQ", "currency": "USD", "action": "BUY", "quantity": "3",
        "order_type": "MKT", "time_in_force": "DAY", "outside_rth": False,
        "status": "Submitted", "warning": "",
    }]
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    run_id = "run-recovery-active-buy"
    journal.create_run(
        run_id=run_id, deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint={"event_count": 0, "close_total": "0"},
    )
    prior_bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 19),
        start_at=datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="recovery-fixture", adjustment="unadjusted",
    )
    retry_bar = prior_bar.model_copy(update={
        "session_date": date(2026, 10, 20),
        "start_at": datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        "end_at": datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        "available_at": datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        "source": "recovery-retry-fixture",
    })
    prior_event = journal.stage_next_event(
        run_id, {"bar": prior_bar.model_dump(mode="json")}, event_id=f"{run_id}:event-1",
    )
    journal.release_next_event(run_id)
    journal.commit_result(run_id, {
        "event_id": prior_event.event_id,
        "state": {"event_count": 1, "close_total": "100", "last_close": "100"},
        "order_proposals": [{"side": "BUY", "integer_qty": 3}],
    })
    retry_event = journal.stage_next_event(
        run_id, {"bar": retry_bar.model_dump(mode="json")}, event_id=f"{run_id}:event-2",
    )
    journal.release_next_event(run_id)

    # Seed the exact linked acknowledged partial BUY required for native ref=77 recovery.
    intent = ledger.create_intent(OrderIntentInput(
        decision_id="recovery-active-buy-event-1", broker="IBKR", account_ref="paper-session",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=version.version_hash, input_event_seq=1, action_index=0,
        bar_end=prior_bar.end_at, instrument_id="AAPL", side="BUY", integer_qty=3,
        reference_price=Decimal("100"), eligible_at=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(days=1), risk_snapshot_id="b" * 64,
    ))
    intent_input = ledger.get_intent(intent.intent_id)
    ledger.reserve(intent.intent_id, "recovery-active-reservation", "202")
    ledger.start_send(
        intent.intent_id, attempt_id="recovery-active-attempt", client_order_id="recovery-active-client",
    )
    index.bind(BrokerOrderBinding(
        broker_order_id=5, account_ref="paper-session", environment=Environment.PAPER,
        intent_id=intent.intent_id, bt_order_ref=77, requested_qty=3, side="BUY",
        contract_id=123, symbol="AAPL", security_type="STK", currency="USD",
    ))
    ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "recovery-active-ack",
        "kind": BrokerEventKind.ACK, "broker_order_id": "5",
        "occurred_at": NOW - timedelta(minutes=2), "received_at": NOW - timedelta(minutes=2),
    })
    ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "recovery-active-fill-1",
        "kind": BrokerEventKind.FILL, "broker_order_id": "5", "broker_exec_id": "recovery-exec-1",
        "quantity": 1, "price": Decimal("101"),
        "occurred_at": datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc),
        "received_at": NOW - timedelta(minutes=1),
    })
    ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "recovery-active-fill-2",
        "kind": BrokerEventKind.FILL, "broker_order_id": "5", "broker_exec_id": "recovery-exec-2",
        "quantity": 1, "price": Decimal("103"),
        "occurred_at": datetime(2026, 10, 20, 15, 1, tzinfo=timezone.utc),
        "received_at": NOW - timedelta(minutes=1),
    })
    journal.prepare_outbox_intent(
        run_id, 1, 0, intent=intent_input.model_dump(mode="json"), decision={"allowed": True},
    )
    journal.link_outbox_intent(run_id, 1, 0, intent.intent_id)
    sandbox = StatefulCheckpointSandbox(
        proposals=[{"side": "BUY", "integer_qty": 3}],
        proposal_event_ids={f"{run_id}:event-1"},
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("no recovered CREATED order needs risk"),
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc),
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id=run_id, version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01,
        now=lambda: datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc),
    )

    def wait_for(predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        assert predicate(), message

    def checkpoint():
        # Read-only polling avoids BEGIN IMMEDIATE contention with journal writes.
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT checkpoint_json FROM execution_run WHERE run_id=?", (run_id,),
            ).fetchone()
        return json.loads(row[0])

    try:
        snapshot = session.start_paused()
        assert snapshot["reconciled"] is True
        restored = session.broker.orders[77]
        assert restored.status == bt.Order.Partial
        assert restored.executed.size == 2
        assert restored.executed.price == pytest.approx(102.0)
        assert len(restored.executed.exbits) == 2
        assert [bit.size for bit in restored.executed.exbits] == [1, 1]
        assert [bit.price for bit in restored.executed.exbits] == [101.0, 103.0]
        assert [bt.num2date(bit.dt).minute for bit in restored.executed.exbits] == [0, 1]
        assert session.broker.getposition(feed).size == 2
        assert runtime.process.order_sends == []

        # Inject the failure at the real Cerebro/bridge boundary while the exact
        # released retry event and active BUY are both durable. The service gate
        # is already closed, so the bridge fails closed before calling Python.
        service.pause(reason="injected_recovery_boundary_failure")
        feed.push_completed_bar(retry_bar)
        wait_for(lambda: session.exception is not None, "paused Cerebro did not fail closed")
        wait_for(lambda: session._thread is not None and not session._thread.is_alive(),
                 "failed Cerebro thread did not exit")
        assert service.state()[0] is ExecutionControlState.PAUSED
        failed_event = journal.inspect_paused_released_event(run_id)
        assert failed_event.event_id == retry_event.event_id
        assert failed_event.seq == retry_event.seq == 2
        assert failed_event.payload == {"bar": retry_bar.model_dump(mode="json")}
        assert feed.current_completed_bar == retry_bar
        assert len(sandbox.requests) == 1
        assert sandbox.requests[0]["event_id"] == prior_event.event_id
        assert sandbox.requests[0]["state"] == {"event_count": 0, "close_total": "0"}

        # Re-envelope the exact first execution: the ledger must dedupe it,
        # while BrokerOrderIndex keeps this separate delivery unACKed for recovery.
        assert runtime.callback_worker.pause(timeout=1) is True
        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 2, "execution_id": "recovery-exec-1", "order_id": 5,
            "account": "paper-session", "side": "BOT", "shares": "1", "price": "101",
            "time": "20261020  11:00:00", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))
        runtime.callback_worker.resume()
        wait_for(lambda: len(index.pending_inbox()) == 1,
                 "duplicate cumulative callback did not create an unACKed durable delivery")
        wait_for(lambda: len(index.pending_delivery_event_ids()) == 1,
                 "duplicate cumulative callback did not bind its durable delivery identity")
        pending_ids = index.pending_delivery_event_ids()
        assert len(ledger.list_fills(intent.intent_id)) == 2

        runtime.process.snapshot_positions = [{
            "request_id": 1900000002, "account": "paper-session", "contract_id": 123,
            "symbol": "AAPL", "currency": "USD", "quantity": "2", "average_cost": "102",
        }]
        runtime.snapshot_id_factory = lambda: 1900000002
        recovery_snapshot = session.recover_failed_session()
        assert recovery_snapshot["reconciled"] is True
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert session.broker.orders[77].ref == 77
        assert session.broker.orders[77].status == bt.Order.Partial
        assert session.broker.orders[77].executed.size == 2
        assert [bit.price for bit in session.broker.orders[77].executed.exbits] == [101.0, 103.0]
        assert session.broker.getposition(session.feeds["AAPL"]).size == 2
        assert index.pending_delivery_event_ids() == pending_ids
        assert len(ledger.list_fills(intent.intent_id)) == 2
        assert runtime.process.order_sends == []
        assert journal.inspect_paused_released_event(run_id) == failed_event
        assert len(sandbox.requests) == 2, "recovery shadow replays the committed prefix without activating"
        assert sandbox.requests[1]["event_id"] == prior_event.event_id

        # Cerebro's idle live-feed loop applies and ACKs the replay while paused.
        wait_for(lambda: index.pending_inbox() == [], "restored native ref did not ACK replay")
        assert session.broker.orders[77].executed.size == 2
        assert len(ledger.list_fills(intent.intent_id)) == 2
        assert service.state()[0] is ExecutionControlState.PAUSED

        # A distinct final fill also reaches ref=77 while paused, so activation
        # can safely retry the same event after the active order is terminal.
        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 3, "execution_id": "recovery-exec-3", "order_id": 5,
            "account": "paper-session", "side": "BOT", "shares": "1", "price": "104",
            "time": "20261021  11:00:00", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))
        wait_for(lambda: ledger.get_order(intent.intent_id).filled_qty == 3,
                 "later fill did not update the durable order")
        wait_for(lambda: session.broker.orders[77].status == bt.Order.Completed,
                 "later fill was not applied to native order ref=77")
        wait_for(lambda: not index.pending_inbox(), "final fill callback was not ACKed")
        assert session.broker.getposition(session.feeds["AAPL"]).size == 3
        assert len(ledger.list_fills(intent.intent_id)) == 3
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert len(sandbox.requests) == 2

        session.activate()
        wait_for(lambda: checkpoint() == {
            "event_count": 2, "close_total": "200", "last_close": "100",
        }, "same released event did not commit after separate activation")
        assert len(sandbox.requests) == 3
        assert sandbox.requests[2]["event_id"] == failed_event.event_id
        assert sandbox.requests[2]["event"]["bar"] == retry_bar.model_dump(mode="json")
        assert session.broker.orders[77].ref == 77
        assert session.broker.orders[77].executed.size == 3
        assert session.broker.getposition(session.feeds["AAPL"]).size == 3
        assert len(ledger.list_fills(intent.intent_id)) == 3
        assert runtime.process.order_sends == []
        assert session.stop(timeout=2) is True
        assert runtime.stop() is True
        assert service.state()[0] is ExecutionControlState.STOPPED
    finally:
        if service.lease_held:
            runtime.stop()
        allocator.close()


def test_unexpected_cerebro_failure_pauses_journal_and_service_without_leaking_details(tmp_path):
    database = tmp_path / "managed-failure.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-failure", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("no order requires a risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("dispatch port must own sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-failure", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    runtime.start_paused()
    service.activate()

    def fail_run():
        raise RuntimeError("private transport detail")

    session.cerebro = SimpleNamespace(run=fail_run)
    session._run_cerebro()

    assert isinstance(session.exception, RuntimeError)
    assert session.health_error_code == "managed_python_session_failed"
    assert "private transport detail" not in session.health_error_code
    assert service.state() == (ExecutionControlState.PAUSED, "managed_python_strategy_failure")
    with sqlite3.connect(database) as db:
        run_state = db.execute(
            "SELECT state FROM execution_run WHERE run_id='run-failure'"
        ).fetchone()
    assert run_state == ("PAUSED",)
    assert runtime.stop() is True
    allocator.close()


def test_fatal_callback_worker_failure_pauses_session_journal_and_scope(tmp_path):
    database = tmp_path / "managed-callback-failure.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-callback-failure", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("no order requires a risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("dispatch port must own sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-callback-failure", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    try:
        session.start_paused()
        session.activate()
        # Inject after handshake/reconciliation so the dedicated callback
        # worker, rather than the Cerebro thread, encounters the fatal fact.
        runtime.process.callbacks.put(SimpleNamespace(
            kind="managed_accounts", payload=(("accounts", "unrelated-account"),),
        ))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and runtime.callback_worker.failure is None:
            time.sleep(0.005)

        assert runtime.callback_worker.failure is not None
        expected_service_state = (
            ExecutionControlState.PAUSED, "ibkr_callback_consumer_failed",
        )
        deadline = time.monotonic() + 2
        run_state = None
        while time.monotonic() < deadline:
            with sqlite3.connect(database) as db:
                run_state = db.execute(
                    "SELECT state FROM execution_run WHERE run_id='run-callback-failure'"
                ).fetchone()
            if service.state() == expected_service_state and run_state == ("PAUSED",):
                break
            time.sleep(0.005)

        assert session.exception is runtime.callback_worker.failure
        assert session.health_error_code == "managed_python_session_failed"
        assert "unrelated-account" not in session.health_error_code
        assert service.state() == expected_service_state
        assert run_state == ("PAUSED",)
    finally:
        # A fatal callback deliberately keeps the production owner lease for
        # reconciliation. This fake-only teardown closes local test threads
        # and releases its temporary lock after preserving the fail-closed
        # assertions above.
        feed.stop_feed()
        if session._thread is not None:
            session._thread.join(timeout=2)
        runtime.health_monitor.stop(timeout=1)
        runtime.callback_worker.stop(timeout=1)
        runtime.process.close(timeout=1)
        if service._lease is not None:
            service._lease.close()
            service._lease = None
        allocator.close()


def test_managed_session_runs_risked_order_through_fake_ibkr_callback_into_backtrader(tmp_path):
    database = tmp_path / "managed-order.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-order", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(
        journal=journal, versions=versions,
        sandbox=NoProposalSandbox([{"side": "BUY", "integer_qty": 2, "reason": "entry"}]),
    )
    grant = RiskGrant.model_validate({
        "grant_id": "grant-managed", "user_action_id": "approval-managed",
        "strategy_version_hash": version.version_hash, "broker": "IBKR",
        "account_ref": "paper-session", "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "50000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })

    def authorize(proposal):
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
            deployment_id="deployment-1", strategy_version_hash=version.version_hash,
            input_event_seq=proposal.seq, action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at, instrument_id="AAPL", side="BUY", integer_qty=2,
            reference_price=Decimal("100"), eligible_at=NOW,
            expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="a" * 64,
        )
        decision = RiskDecision(
            allowed=True, reason_codes=(), checked_at=NOW, risk_snapshot_id="a" * 64,
            estimated_notional=Decimal("200"), estimated_cost=Decimal("200"),
            projected_strategy_exposure=Decimal("200"), projected_gross_exposure=Decimal("200"),
        )
        return AuthorizedProposal(intent, decision, "IBKR", Decimal("100"))

    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        return DispatchRiskSnapshot(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=Decimal("100"),
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"), available_cash=Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("100")),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
                orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="deployment-1",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process,
        intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-order", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
    manager = PaperExecutionManager(
        runtime_factory=lambda requested_scope: session,
        control_authorizer=lambda principal, requested_scope, action: (
            principal.attributable and requested_scope == scope and action in {"activate", "stop"}
        ),
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    manager.start(scope)
    principal = Principal(subject="managed-session-human", auth_method=AuthMethod.FEDERATED_IDENTITY)
    manager.activate(scope, principal)
    session.push_completed_bar(Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=NOW.replace(day=20, hour=13, minute=30),
        end_at=NOW.replace(day=20, hour=20), available_at=NOW.replace(day=20, hour=20),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("102"),
        low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    ))
    deadline = time.monotonic() + 3
    native_order = None
    while time.monotonic() < deadline:
        if session.exception is not None:
            raise session.exception
        if session.broker is not None and session.broker.orders:
            native_order = next(iter(session.broker.orders.values()))
            if native_order.status == native_order.Completed:
                break
        time.sleep(0.005)

    assert session.exception is None
    with sqlite3.connect(database) as db:
        run_cursor = db.execute("SELECT next_seq FROM execution_run WHERE run_id='run-order'").fetchone()
    assert len(runtime.process.order_sends) == 1, (
        f"Cerebro did not dispatch: cursor={run_cursor}, thread_alive={session.is_alive}, "
        f"feed_bar={feed.current_completed_bar}, broker_orders={getattr(session.broker, 'orders', None)}, "
        f"order_status={[order.getstatusname() for order in session.broker.orders.values()]}, "
        f"ledger={ledger.recovery_snapshot_for_scope(broker='IBKR', account_ref='paper-session', environment=Environment.PAPER)}"
    )
    assert native_order is not None and native_order.status == native_order.Completed, (
        f"status={None if native_order is None else native_order.getstatusname()}, "
        f"ledger={ledger.recovery_snapshot_for_scope(broker='IBKR', account_ref='paper-session', environment=Environment.PAPER)}, "
        f"inbox={index.pending_inbox()}, callbacks={index.pending_callbacks()}, "
        f"router={session.callback_router.pending_count}, broker_queue={session.broker._callback_queue.qsize()}, "
        f"worker_error={runtime.callback_worker.failure}, pump_error={runtime.callback_pump._fatal}"
    )
    assert native_order.executed.size == 2
    assert session.broker.getposition(session.feeds["AAPL"]).size == 2
    assert session.broker.getcash() == pytest.approx(8798.0)
    # Native Order completion is applied before the deferred durable inbox ACK
    # is committed. Observe that acknowledgment explicitly instead of racing
    # the callback worker at the end of this asynchronous integration path.
    ack_deadline = time.monotonic() + 3
    while index.pending_inbox() and time.monotonic() < ack_deadline:
        if session.exception is not None:
            raise session.exception
        time.sleep(0.005)
    recovery = ledger.recovery_snapshot_for_scope(
        broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
    )
    assert recovery.unresolved_sends == () and recovery.active_orders == ()
    intent_id = next(iter(session.port._native_bindings.values()))
    assert ledger.get_order(intent_id).filled_qty == 2
    assert index.pending_inbox() == []
    assert manager.stop(scope, principal) is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    allocator.close()


def test_managed_session_runs_frozen_nl2_sma_target_fraction_through_native_backtrader_order(tmp_path):
    database = tmp_path / "managed-nl2-sma.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process,
        intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
    from src.bt_runtime.strategies.sma import (
        TRUSTED_TEMPLATE_COMPILER_VERSION, TRUSTED_TEMPLATE_SOURCE,
    )

    spec = StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "nl2-sma-paper",
        "source_kind": "natural_language_template", "template_id": "sma_cross", "template_version": "1.0",
        "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
        "parameters": {"fast_period": 1, "slow_period": 2},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                             "allocation_fraction": "0.2", "rebalance": "on_signal_transition",
                             "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "skip", "missed_session_action": "expire"},
    })
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        spec, template_source=TRUSTED_TEMPLATE_SOURCE,
        engine_version="backtrader/1.9.78.123", compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-nl2-paper", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    grant = RiskGrant.model_validate({
        "grant_id": "grant-nl2", "user_action_id": "approval-nl2",
        "strategy_version_hash": version.version_hash, "broker": "IBKR", "account_ref": "paper-session",
        "environment": "paper", "allowed_instruments": ["AAPL"], "allowed_order_types": ["market"],
        "long_only": True, "whole_shares": True, "max_order_notional": "10000",
        "max_gross_exposure": "100000", "strategy_budget": "5000", "max_daily_loss": "1000",
        "max_orders_per_session": 10, "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(hours=2), "quote_max_age_seconds": 30,
        "account_max_age_seconds": 60, "halt_action": "pause_and_cancel_owned",
        "liquidation_policy": "none", "granted_at": NOW,
    })

    def risk_inputs(proposal):
        return ProposalRiskInputs(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=proposal.bar.close,
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"), available_cash=Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=proposal.bar.close),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
                orders_this_session=0, account_snapshot_complete=True,
            ),
            eligible_at=NOW, expires_at=NOW + timedelta(minutes=30),
        )

    sizer = TargetFractionPaperSizer(risk_inputs)
    coordinator = TrustedTemplateEventCoordinator(journal=journal, versions=versions, size_signal=sizer)
    handoff = PythonOutboxIntentHandoff(
        journal=journal, ledger=ledger, authorize=RiskAwareProposalAuthorizer(risk_inputs),
    )

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        return DispatchRiskSnapshot(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id=intent.instrument_id, now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=intent.reference_price,
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"), available_cash=Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=intent.reference_price),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
                orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="deployment-1",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: datetime(2026, 10, 23, 22, tzinfo=timezone.utc),
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-nl2-paper", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01,
        now=lambda: datetime(2026, 10, 23, 22, tzinfo=timezone.utc),
    )

    session.start_paused()
    runtime.activate()
    for day, close in ((20, "10"), (21, "9"), (22, "11")):
        session.push_completed_bar(Bar(
            instrument_id="AAPL", session_date=date(2026, 10, day),
            start_at=datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
            available_at=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
            timezone="America/New_York", open=Decimal(close), high=Decimal(close), low=Decimal(close),
            close=Decimal(close), volume=Decimal("1000"), currency="USD", is_final=True,
            source="fixture", adjustment="unadjusted",
        ))
        deadline = time.monotonic() + 2
        expected_cursor = day - 19
        while time.monotonic() < deadline:
            with sqlite3.connect(database) as db:
                cursor = db.execute(
                    "SELECT next_seq FROM execution_run WHERE run_id='run-nl2-paper'"
                ).fetchone()
            if cursor and cursor[0] == expected_cursor + 1:
                break
            time.sleep(0.005)
        assert cursor == (expected_cursor + 1,)

    deadline = time.monotonic() + 3
    native_order = None
    while time.monotonic() < deadline:
        if session.exception is not None:
            raise session.exception
        if session.broker is not None and session.broker.orders:
            native_order = next(iter(session.broker.orders.values()))
            if native_order.status == native_order.Completed:
                break
        time.sleep(0.005)
    assert session.exception is None
    assert len(runtime.process.order_sends) == 1
    assert runtime.process.order_sends[0]["order"]["quantity"] == 180
    assert native_order is not None and native_order.status == native_order.Completed
    assert native_order.executed.size == 180
    assert session.broker.getposition(session.feeds["AAPL"]).size == 180
    assert session.stop(timeout=2) is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    allocator.close()


def test_managed_session_runs_frozen_nl2_rsi_entry_and_exit_through_native_orders(tmp_path):
    """RSI entry/exit share the trusted template's durable managed PAPER path."""
    database = tmp_path / "managed-nl2-rsi.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process,
        intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
    from src.bt_runtime.strategies.sma import (
        TRUSTED_TEMPLATE_COMPILER_VERSION, TRUSTED_TEMPLATE_SOURCE,
    )

    spec = StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "nl2-rsi-paper",
        "source_kind": "natural_language_template", "template_id": "rsi_threshold", "template_version": "1.0",
        "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
        "parameters": {"period": 2, "entry_below": 30, "exit_above": 70},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                             "allocation_fraction": "0.2", "rebalance": "on_signal_transition",
                             "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "skip", "missed_session_action": "expire"},
    })
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        spec, template_source=TRUSTED_TEMPLATE_SOURCE,
        engine_version=f"backtrader/{bt.__version__}", compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-nl2-rsi-paper", deployment_id="deployment-rsi", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    grant = RiskGrant.model_validate({
        "grant_id": "grant-nl2-rsi", "user_action_id": "approval-nl2-rsi",
        "strategy_version_hash": version.version_hash, "broker": "IBKR", "account_ref": "paper-session",
        "environment": "paper", "allowed_instruments": ["AAPL"], "allowed_order_types": ["market"],
        "long_only": True, "whole_shares": True, "max_order_notional": "10000",
        "max_gross_exposure": "100000", "strategy_budget": "5000", "max_daily_loss": "1000",
        "max_orders_per_session": 10, "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(hours=2), "quote_max_age_seconds": 30,
        "account_max_age_seconds": 60, "halt_action": "pause_and_cancel_owned",
        "liquidation_policy": "none", "granted_at": NOW,
    })
    strategy_qty = {"value": 0}

    def make_risk_facts(proposal, quote_price):
        held = strategy_qty["value"]
        return RiskFacts(
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
            strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
            quote_as_of=NOW, account_as_of=NOW, quote_price=quote_price,
            price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
            net_liquidation=Decimal("10000"), available_cash=Decimal("9000") if held == 0 else Decimal("7000"),
            positions=(PositionFact(instrument_id="AAPL", quantity=held, market_price=quote_price),),
            strategy_position_qty=held, strategy_buy_reservations=Decimal("0"),
            account_buy_reservations=Decimal("0"), strategy_sell_reservations=0,
            strategy_daily_loss=Decimal("0"), orders_this_session=len(runtime.process.order_sends),
            account_snapshot_complete=True,
        )

    def risk_inputs(proposal):
        return ProposalRiskInputs(
            grant=grant, facts=make_risk_facts(proposal, proposal.bar.close),
            eligible_at=NOW, expires_at=NOW + timedelta(minutes=30),
        )

    coordinator = TrustedTemplateEventCoordinator(
        journal=journal, versions=versions, size_signal=TargetFractionPaperSizer(risk_inputs),
    )
    handoff = PythonOutboxIntentHandoff(
        journal=journal, ledger=ledger, authorize=RiskAwareProposalAuthorizer(risk_inputs),
    )

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        proposal = SimpleNamespace(bar=SimpleNamespace(close=intent.reference_price))
        return DispatchRiskSnapshot(
            grant=grant, facts=make_risk_facts(proposal, intent.reference_price),
            deployment_id="deployment-rsi",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    session_now = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: session_now,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-nl2-rsi-paper", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: session_now,
    )

    def wait_for_cursor(expected_next_seq):
        deadline = time.monotonic() + 3
        cursor = None
        while time.monotonic() < deadline:
            if session.exception is not None:
                raise session.exception
            with sqlite3.connect(database) as db:
                cursor = db.execute(
                    "SELECT next_seq FROM execution_run WHERE run_id='run-nl2-rsi-paper'"
                ).fetchone()
            if cursor == (expected_next_seq,):
                return
            time.sleep(0.005)
        pytest.fail(f"RSI managed event cursor did not reach {expected_next_seq}: {cursor}")

    def wait_for_completed_order(count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if session.exception is not None:
                raise session.exception
            completed = [order for order in session.broker.orders.values()
                         if order.status == order.Completed] if session.broker is not None else []
            if len(runtime.process.order_sends) >= count and len(completed) >= count and not index.pending_inbox():
                return completed
            time.sleep(0.005)
        pytest.fail(
            f"RSI native orders did not complete: sends={runtime.process.order_sends}, "
            f"orders={getattr(session.broker, 'orders', None)}, inbox={index.pending_inbox()}"
        )

    session.start_paused()
    runtime.activate()
    closes = ("100", "105", "110", "105", "100", "95", "100", "105")
    session_days = (13, 14, 15, 16, 19, 20, 21, 22)
    orders_after_entry = None
    for index_in_series, close in enumerate(closes):
        day = session_days[index_in_series]
        value = Decimal(close)
        session.push_completed_bar(Bar(
            instrument_id="AAPL", session_date=date(2026, 10, day),
            start_at=datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
            available_at=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
            timezone="America/New_York", open=value, high=value, low=value, close=value,
            volume=Decimal("1000"), currency="USD", is_final=True,
            source="fixture", adjustment="unadjusted",
        ))
        wait_for_cursor(index_in_series + 2)
        if index_in_series == 4:
            completed = wait_for_completed_order(1)
            assert completed[0].isbuy() and completed[0].executed.size == 19
            assert runtime.process.order_sends[0]["order"]["action"] == "BUY"
            assert runtime.process.order_sends[0]["order"]["quantity"] == 19
            assert session.broker.getposition(session.feeds["AAPL"]).size == 19
            strategy_qty["value"] = 19
            orders_after_entry = completed
        elif index_in_series == 7:
            completed = wait_for_completed_order(2)
            sells = [order for order in completed if order.issell()]
            assert len(sells) == 1 and sells[0].executed.size == -19
            assert [payload["order"]["action"] for payload in runtime.process.order_sends] == ["BUY", "SELL"]
            assert [payload["order"]["quantity"] for payload in runtime.process.order_sends] == [19, 19]
            assert session.broker.getposition(session.feeds["AAPL"]).size == 0
            assert len(orders_after_entry) == 1

    assert session.exception is None
    recovery = ledger.recovery_snapshot_for_scope(
        broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
    )
    assert recovery.unresolved_sends == () and recovery.active_orders == ()
    assert index.pending_inbox() == []
    assert session.stop(timeout=2) is True
    assert service.state()[0] is ExecutionControlState.STOPPED
    allocator.close()


def test_blocked_managed_strategy_still_persists_fill_and_authorized_exact_cancel(tmp_path):
    database = tmp_path / "blocked-managed-order.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    runtime.process.auto_ack_on_send = True
    runtime.process.auto_fill_on_send = False
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process,
        intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)

    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-blocked", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    sandbox = BlockingFirstEventSandbox()
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    grant = RiskGrant.model_validate({
        "grant_id": "grant-blocked", "user_action_id": "approval-blocked",
        "strategy_version_hash": version.version_hash, "broker": "IBKR",
        "account_ref": "paper-session", "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "50000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })

    def authorize(proposal):
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
            deployment_id="deployment-1", strategy_version_hash=version.version_hash,
            input_event_seq=proposal.seq, action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at, instrument_id="AAPL", side="BUY", integer_qty=2,
            reference_price=Decimal("100"), eligible_at=NOW,
            expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="d" * 64,
        )
        decision = RiskDecision(
            allowed=True, reason_codes=(), checked_at=NOW, risk_snapshot_id="d" * 64,
            estimated_notional=Decimal("200"), estimated_cost=Decimal("200"),
            projected_strategy_exposure=Decimal("200"), projected_gross_exposure=Decimal("200"),
        )
        return AuthorizedProposal(intent, decision, "IBKR", Decimal("100"))

    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        return DispatchRiskSnapshot(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=Decimal("100"),
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"), available_cash=Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("100")),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
                orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="deployment-1",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-blocked", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )
    scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
    manager = PaperExecutionManager(
        runtime_factory=lambda requested_scope: session,
        control_authorizer=lambda principal, requested_scope, action: (
            principal.attributable and requested_scope == scope and action in {"activate", "cancel", "stop"}
        ),
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    principal = Principal(subject="blocked-session-human", auth_method=AuthMethod.FEDERATED_IDENTITY)
    feed_started = False
    try:
        manager.start(scope)
        manager.activate(scope, principal)

        def push_bar(day: int) -> None:
            session.push_completed_bar(Bar(
                instrument_id="AAPL", session_date=date(2026, 10, day),
                start_at=NOW.replace(day=day, hour=13, minute=30),
                end_at=NOW.replace(day=day, hour=20), available_at=NOW.replace(day=day, hour=20),
                timezone="America/New_York", open=Decimal("100"), high=Decimal("102"),
                low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
                currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
            ))

        def wait_for(predicate, message: str, timeout: float = 3.0) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if session.exception is not None:
                    raise session.exception
                if predicate():
                    return
                time.sleep(0.005)
            pytest.fail(message)

        def next_seq() -> int:
            with sqlite3.connect(database) as db:
                return db.execute(
                    "SELECT next_seq FROM execution_run WHERE run_id='run-blocked'"
                ).fetchone()[0]

        push_bar(19)
        feed_started = True
        assert sandbox.entered.wait(3), "strategy event did not enter its blocked sandbox call"
        assert session.is_alive is True
        assert sandbox.release.is_set() is False
        with sqlite3.connect(database) as db:
            cursor = db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='run-blocked'"
            ).fetchone()[0]
            staged = db.execute(
                "SELECT seq, committed_at FROM strategy_event WHERE run_id='run-blocked' ORDER BY seq"
            ).fetchall()
        assert cursor == 1 and staged[-1][0] == 1 and staged[-1][1] is None
        assert journal.list_pending_outbox("run-blocked") == []
        assert runtime.process.order_sends == [], "an uncommitted strategy proposal must not dispatch"

        # Model one already-owned broker order while the strategy worker is
        # stalled. The durable binding and native order are test fixtures;
        # callbacks and cancellation below still use production paths.
        owned = ledger.create_intent(OrderIntentInput(
            decision_id="prior-owned-order", broker="IBKR", account_ref="paper-session",
            environment=Environment.PAPER, deployment_id="deployment-1",
            strategy_version_hash=version.version_hash, input_event_seq=1, action_index=0,
            bar_end=NOW - timedelta(days=1), instrument_id="AAPL", side="BUY", integer_qty=2,
            reference_price=Decimal("100"), eligible_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="e" * 64,
        ))
        ledger.reserve(owned.intent_id, "prior-owned-reservation", "202")
        ledger.start_send(
            owned.intent_id, attempt_id="prior-owned-attempt", client_order_id="prior-owned-client",
        )
        index.bind(BrokerOrderBinding(
            broker_order_id=55, account_ref="paper-session", environment=Environment.PAPER,
            intent_id=owned.intent_id, bt_order_ref=77, requested_qty=2, side="BUY",
            contract_id=123, symbol="AAPL", security_type="STK", currency="USD",
        ))
        ledger.record_broker_event({
            "broker": "IBKR", "account_ref": "paper-session", "environment": "paper",
            "intent_id": owned.intent_id, "broker_event_id": "prior-owned-ack",
            "kind": BrokerEventKind.ACK, "broker_order_id": "55",
            "occurred_at": NOW - timedelta(minutes=2), "received_at": NOW - timedelta(minutes=2),
        })
        native_order = bt.BuyOrder(
            owner=None, data=feed, size=2, exectype=bt.Order.Market, simulated=True, price=100,
        )
        native_order.ref = 77
        native_order.submit(session.broker)
        native_order.accept(session.broker)
        session.broker._register_order(native_order)

        intent_id = owned.intent_id
        broker_order_id = 55
        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 1, "execution_id": "blocked-partial-fill", "order_id": broker_order_id,
            "account": "paper-session", "side": "BOT", "shares": "1", "price": "101",
            "time": "20261021  06:00:00", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))
        wait_for(
            lambda: ledger.get_order(intent_id).filled_qty == 1,
            "independent callback worker did not persist partial fill while strategy was blocked",
        )
        assert session.is_alive is True and sandbox.release.is_set() is False
        assert runtime.callback_worker.alive is True
        assert len(index.pending_inbox()) == 1, "fill must remain queued for the blocked Cerebro thread"

        untrusted = Principal(subject="unattributed", auth_method=AuthMethod.SHARED_KEY)
        with pytest.raises(PaperRuntimeAuthorizationError):
            manager.cancel_owned_order(scope, intent_id, untrusted)
        assert runtime.process.cancel_calls == []

        canceled = manager.cancel_owned_order(scope, intent_id, principal)
        assert canceled.state.value == "CANCEL_PENDING"
        assert runtime.process.cancel_calls == [broker_order_id]
        runtime.process.callbacks.put(SimpleNamespace(kind="order_status", payload=tuple(sorted({
            "request_id": 1, "order_id": broker_order_id, "status": "Cancelled",
            "filled": "1", "remaining": "1",
        }.items()))))
        wait_for(
            lambda: ledger.get_order(intent_id).state.value == "CANCELLED",
            "broker cancellation callback was not persisted",
        )
        assert ledger.get_order(intent_id).filled_qty == 1
        assert runtime.process.order_sends == []
        assert session.is_alive is True and sandbox.release.is_set() is False

        runtime.process.auto_fill_on_send = True
        sandbox.release.set()
        wait_for(
            lambda: next_seq() == 2,
            "strategy event did not commit after the blocked sandbox was released",
        )
        wait_for(
            lambda: len(runtime.process.order_sends) == 1,
            "committed strategy proposal was not dispatched after the sandbox returned",
        )
        strategy_intent_id = next(
            intent for order_ref, intent in session.port._native_bindings.items()
            if order_ref != native_order.ref
        )
        wait_for(
            lambda: ledger.get_order(strategy_intent_id).state.value == "FILLED",
            "post-commit fake strategy order did not complete",
        )
        wait_for(lambda: index.pending_inbox() == [], "queued callback was not acknowledged by Cerebro")
        assert session.exception is None
        assert len(runtime.process.order_sends) == 1
        assert native_order.status == bt.Order.Canceled
        assert native_order.executed.size == 1
        assert manager.stop(scope, principal) is True
        assert service.state()[0] is ExecutionControlState.STOPPED
    finally:
        sandbox.release.set()
        if feed_started and session._thread is not None:
            session._thread.join(timeout=2)
        runtime.health_monitor.stop(timeout=1)
        runtime.callback_worker.stop(timeout=1)
        runtime.process.close(timeout=1)
        if service._lease is not None:
            service._lease.close()
            service._lease = None
        allocator.close()


@pytest.mark.integration
def test_managed_session_real_worker_timeout_pauses_run_but_keeps_callback_and_cancel_alive(
    tmp_path, monkeypatch,
):
    context = os.environ.get("DOCKER_CONTEXT", "colima-vibe-execution")
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)

    def docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["docker", "--context", context, *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=docker_env, timeout=timeout, check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:2000]
            pytest.fail(f"Docker command failed ({result.returncode}): {args!r}: {detail}")
        return result

    docker("info", "--format", "{{.ServerVersion}}")
    worker_dir = Path(__file__).parents[2] / "src" / "execution" / "strategy_worker_image"
    image_tag = f"vibe-strategy-worker-managed-timeout:{uuid.uuid4().hex}"
    image_built = False
    runtime = service = session = feed = allocator = None
    try:
        docker(
            "build", "--network=none", "--pull=false", "--file", str(worker_dir / "Dockerfile"),
            "--tag", image_tag, str(worker_dir), timeout=120,
        )
        image_built = True
        image_id = docker("image", "inspect", "--format", "{{.Id}}", image_tag).stdout.decode().strip()

        database = tmp_path / "managed-real-timeout.sqlite"
        runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            _spec(), upload_source=b"def on_event(event, state):\n    while True: pass\n",
            engine_version=f"backtrader/{bt.__version__}", compiler_version="vibe-python-event/1",
        )
        journal = SQLiteEventJournal(database)
        journal.create_run(
            run_id="run-real-timeout", deployment_id="deployment-timeout",
            account_ref="paper-session", environment=Environment.PAPER,
            strategy_version_hash=version.version_hash,
        )
        coordinator = PythonEventCoordinator(
            journal=journal, versions=versions,
            sandbox=PythonSandbox(
                image_id, context=context,
                limits=SandboxLimits(timeout_seconds=2, max_request_bytes=512_000),
            ),
        )
        handoff = PythonOutboxIntentHandoff(
            journal=journal, ledger=ledger, authorize=lambda _proposal: None,
        )
        dispatch = PaperOrderDispatchService(
            ledger=ledger,
            facts_provider=lambda *_args, **_kwargs: pytest.fail("timed-out strategy must not request order risk facts"),
            sender=lambda *_args, **_kwargs: pytest.fail("managed session must not use a fallback sender"),
            now=lambda: NOW,
        )
        feed = PaperCompletedBarFeed(
            instrument_id="AAPL", timezone="America/New_York", queue_size=4,
            idle_poll_seconds=0.01, now=lambda: NOW,
        )
        session = ManagedPythonPaperSession(
            runtime=runtime, run_id="run-real-timeout", version_store=versions, version_id=version.version_id,
            journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
            feeds={"AAPL": feed}, data_by_con_id={123: feed},
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
            queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
        )
        scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
        manager = PaperExecutionManager(
            runtime_factory=lambda requested_scope: session,
            control_authorizer=lambda principal, requested_scope, action: (
                principal.attributable and requested_scope == scope and action in {"activate", "cancel", "stop"}
            ),
            startup_readiness=PaperExecutionReadiness(
                strategy_session_builder=True,
                attributable_control_authorizer=True,
                account_and_reconciliation_facts=True,
                market_data_and_verified_contracts=True,
                risk_and_cost_facts=True,
            ),
        )
        principal = Principal(subject="real-timeout-test", auth_method=AuthMethod.FEDERATED_IDENTITY)
        manager.start(scope)
        manager.activate(scope, principal)
        session.push_completed_bar(Bar(
            instrument_id="AAPL", session_date=date(2026, 10, 20),
            start_at=NOW.replace(day=20, hour=13, minute=30),
            end_at=NOW.replace(day=20, hour=20), available_at=NOW.replace(day=20, hour=20),
            timezone="America/New_York", open=Decimal("100"), high=Decimal("102"),
            low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
            currency="USD", is_final=True, source="docker-timeout-fixture", adjustment="unadjusted",
        ))

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if session.exception is not None and service.state()[0] is ExecutionControlState.PAUSED:
                with sqlite3.connect(database) as db:
                    state = db.execute(
                        "SELECT state FROM execution_run WHERE run_id='run-real-timeout'"
                    ).fetchone()
                if state == ("PAUSED",):
                    break
            time.sleep(0.01)
        assert session.exception is not None, "Cerebro did not receive the real worker timeout"
        cause: BaseException | None = session.exception
        cause_chain = []
        while cause is not None and not isinstance(cause, SandboxTimeout):
            cause_chain.append(cause)
            cause = cause.__cause__
        if cause is not None:
            cause_chain.append(cause)
        assert isinstance(cause, SandboxTimeout), f"session failure did not preserve SandboxTimeout: {session.exception!r}"
        assert any(isinstance(error, EventWorkerError) for error in cause_chain)
        assert session.health_error_code == "managed_python_session_failed"
        assert manager.status().last_error_code == "managed_python_session_failed"
        assert service.state()[0] is ExecutionControlState.PAUSED
        with pytest.raises(ManagedPythonPaperSessionError, match="rebuild the session and reconcile"):
            manager.activate(scope, principal)
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert runtime.process.order_sends == []
        assert runtime.callback_worker.alive is True
        assert runtime.callback_worker.failure is None

        with sqlite3.connect(database) as db:
            run_state, next_seq = db.execute(
                "SELECT state,next_seq FROM execution_run WHERE run_id='run-real-timeout'"
            ).fetchone()
            released_at, committed_at = db.execute(
                "SELECT released_at,committed_at FROM strategy_event "
                "WHERE run_id='run-real-timeout' AND seq=1"
            ).fetchone()
            result_count = db.execute(
                "SELECT COUNT(*) FROM event_result WHERE run_id='run-real-timeout'"
            ).fetchone()[0]
            outbox_count = db.execute(
                "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-real-timeout'"
            ).fetchone()[0]
        assert (run_state, next_seq) == ("PAUSED", 1)
        assert released_at is not None and committed_at is None
        assert result_count == 0 and outbox_count == 0
        assert runtime.process.order_sends == []

        # The SDK callback pump remains independent from the failed Cerebro
        # thread and continues accepting valid management callbacks in PAUSED.
        runtime.process.callbacks.put(SimpleNamespace(
            kind="next_valid_id", payload=(("order_id", 777),),
        ))
        callback_deadline = time.monotonic() + 3
        observed_candidate = None
        while time.monotonic() < callback_deadline:
            with sqlite3.connect(database) as db:
                row = db.execute(
                    "SELECT next_order_id,candidate FROM ibkr_order_id_state WHERE singleton=1"
                ).fetchone()
            if row == (777, 1):
                observed_candidate = row
                break
            time.sleep(0.005)
        assert observed_candidate == (777, 1), "independent callback worker did not process nextValidId"
        assert runtime.callback_worker.alive is True
        assert service.state()[0] is ExecutionControlState.PAUSED

        # Seed one exact owned order after the strategy failure. Management
        # controls and broker callbacks must remain available while new
        # strategy dispatch is durably paused.
        runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
        owned = ledger.create_intent(OrderIntentInput(
            decision_id="timeout-owned-order", broker="IBKR", account_ref="paper-session",
            environment=Environment.PAPER, deployment_id="deployment-timeout",
            strategy_version_hash=version.version_hash, input_event_seq=1, action_index=0,
            bar_end=NOW - timedelta(days=1), instrument_id="AAPL", side="BUY", integer_qty=2,
            reference_price=Decimal("100"), eligible_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="f" * 64,
        ))
        ledger.reserve(owned.intent_id, "timeout-owned-reservation", "202")
        ledger.start_send(
            owned.intent_id, attempt_id="timeout-owned-attempt", client_order_id="timeout-owned-client",
        )
        index.bind(BrokerOrderBinding(
            broker_order_id=55, account_ref="paper-session", environment=Environment.PAPER,
            intent_id=owned.intent_id, bt_order_ref=77, requested_qty=2, side="BUY",
            contract_id=123, symbol="AAPL", security_type="STK", currency="USD",
        ))
        ledger.record_broker_event({
            "broker": "IBKR", "account_ref": "paper-session", "environment": "paper",
            "intent_id": owned.intent_id, "broker_event_id": "timeout-owned-ack",
            "kind": BrokerEventKind.ACK, "broker_order_id": "55",
            "occurred_at": NOW - timedelta(minutes=2), "received_at": NOW - timedelta(minutes=2),
        })

        # A safe stop is allowed to remain pending while an owned order is
        # active. Risk-reducing cancellation must remain available in that
        # retained STOPPING manager slot.
        assert manager.stop(scope, principal) is False
        pending_stop = manager.status()
        assert pending_stop.phase == "STOPPING"
        assert pending_stop.control_state is ExecutionControlState.PAUSED
        assert pending_stop.owner_lease_held is True

        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 1, "execution_id": "timeout-partial-fill", "order_id": 55,
            "account": "paper-session", "side": "BOT", "shares": "1", "price": "101",
            "time": "20261021  06:00:00", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))
        callback_deadline = time.monotonic() + 3
        while time.monotonic() < callback_deadline:
            if ledger.get_order(owned.intent_id).filled_qty == 1:
                break
            time.sleep(0.005)
        assert ledger.get_order(owned.intent_id).filled_qty == 1
        assert runtime.callback_worker.alive is True
        assert session.broker is not None and session.broker._callback_queue.empty()
        assert len(runtime.order_index.pending_delivery_event_ids()) == 1

        untrusted = Principal(subject="timeout-unattributed", auth_method=AuthMethod.SHARED_KEY)
        with pytest.raises(PaperRuntimeAuthorizationError):
            manager.cancel_owned_order(scope, owned.intent_id, untrusted)
        assert runtime.process.cancel_calls == []
        canceled = manager.cancel_owned_order(scope, owned.intent_id, principal)
        assert canceled.state.value == "CANCEL_PENDING"
        assert runtime.process.cancel_calls == [55]
        runtime.process.callbacks.put(SimpleNamespace(kind="order_status", payload=tuple(sorted({
            "request_id": 1, "order_id": 55, "status": "Cancelled",
            "filled": "1", "remaining": "1",
        }.items()))))
        callback_deadline = time.monotonic() + 3
        while time.monotonic() < callback_deadline:
            if ledger.get_order(owned.intent_id).state.value == "CANCELLED":
                break
            time.sleep(0.005)
        assert ledger.get_order(owned.intent_id).state.value == "CANCELLED"
        assert ledger.get_order(owned.intent_id).filled_qty == 1
        assert runtime.callback_worker.alive is True
        assert session.broker is not None and session.broker._callback_queue.empty()
        assert len(runtime.order_index.pending_delivery_event_ids()) == 2
        assert service.state()[0] is ExecutionControlState.PAUSED
        pending_stop = manager.status()
        assert pending_stop.phase == "STOPPING" and pending_stop.owner_lease_held is True
        assert runtime.process.order_sends == []
    finally:
        if feed is not None:
            feed.stop_feed()
        if session is not None and session._thread is not None:
            session._thread.join(timeout=2)
        if runtime is not None:
            runtime.health_monitor.stop(timeout=1)
            runtime.callback_worker.stop(timeout=1)
            runtime.process.close(timeout=1)
            if service is not None and service._lease is not None:
                service._lease.close()
                service._lease = None
        if allocator is not None:
            allocator.close()
        if image_built:
            docker("image", "rm", "--force", image_tag)


@pytest.mark.parametrize("matching_identity", [True, False])
@pytest.mark.parametrize("outbox_link", ["current_run", "missing", "other_run"])
def test_new_managed_session_restores_partial_buy_from_reopened_sqlite_and_resumes_callback(
    tmp_path, matching_identity, outbox_link,
):
    database = tmp_path / "managed-reopen.sqlite"
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-reopened", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    seed_ledger = SQLiteOrderLedger(database)
    seed_index = BrokerOrderIndex(database)
    intent = seed_ledger.create_intent(OrderIntentInput(
        decision_id="previous-session-order", broker="IBKR", account_ref="paper-session",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=(version.version_hash if matching_identity else "c" * 64),
        input_event_seq=1, action_index=0,
        bar_end=NOW - timedelta(days=1), instrument_id="AAPL", side="BUY", integer_qty=2,
        reference_price=Decimal("100"), eligible_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="b" * 64,
    ))
    intent_input = seed_ledger.get_intent(intent.intent_id)
    seed_ledger.reserve(intent.intent_id, "previous-reservation", "202")
    seed_ledger.start_send(
        intent.intent_id, attempt_id="previous-attempt", client_order_id="previous-client-order",
    )
    seed_index.bind(BrokerOrderBinding(
        broker_order_id=5, account_ref="paper-session", environment=Environment.PAPER,
        intent_id=intent.intent_id, bt_order_ref=77, requested_qty=2, side="BUY",
        contract_id=123, symbol="AAPL", security_type="STK", currency="USD",
    ))
    seed_ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "previous-ack",
        "kind": BrokerEventKind.ACK, "broker_order_id": "5",
        "occurred_at": NOW - timedelta(minutes=2), "received_at": NOW - timedelta(minutes=2),
    })
    seed_ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "previous-exec-1",
        "kind": BrokerEventKind.FILL, "broker_order_id": "5", "broker_exec_id": "old-exec-1",
        "quantity": 1, "price": Decimal("101"),
        "occurred_at": NOW - timedelta(minutes=1), "received_at": NOW - timedelta(minutes=1),
    })
    def commit_order_proposal(run_id: str, *, include_proposal: bool) -> None:
        bar = Bar(
            instrument_id="AAPL", session_date=date(2026, 10, 20),
            start_at=datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
            available_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc), timezone="America/New_York",
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=Decimal("1000"), currency="USD",
            is_final=True, source="managed-reopen-fixture", adjustment="unadjusted",
        )
        event = journal.stage_next_event(
            run_id, {"bar": bar.model_dump(mode="json")}, event_id=f"{run_id}-event",
        )
        journal.release_next_event(run_id)
        journal.commit_result(run_id, {
            "event_id": event.event_id,
            "state": {"events": 1},
            "order_proposals": ([{"side": "BUY", "integer_qty": 2}] if include_proposal else []),
        })

    commit_order_proposal("run-reopened", include_proposal=outbox_link == "current_run")
    if outbox_link == "other_run":
        journal.create_run(
            run_id="run-foreign", deployment_id="deployment-1", account_ref="paper-session",
            environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        )
        commit_order_proposal("run-foreign", include_proposal=True)
    if outbox_link in {"current_run", "other_run"}:
        linked_run_id = "run-reopened" if outbox_link == "current_run" else "run-foreign"
        frozen = journal.prepare_outbox_intent(
            linked_run_id, 1, 0, intent=intent_input.model_dump(mode="json"), decision={"allowed": True},
        )
        assert frozen["intent"] == intent_input.model_dump(mode="json")
        journal.link_outbox_intent(linked_run_id, 1, 0, intent.intent_id)
    # The runtime below opens new SQLite store objects against the same file.
    del seed_index, seed_ledger

    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    runtime.process.next_valid_id = 6
    runtime.process.snapshot_positions = [{
        "request_id": 1900000001, "account": "paper-session", "contract_id": 123,
        "symbol": "AAPL", "currency": "USD", "quantity": "1", "average_cost": "101",
    }]
    runtime.process.snapshot_open_orders = [{
        "order_id": 5, "account": "paper-session", "permanent_id": 50, "contract_id": 123,
        "symbol": "AAPL", "security_type": "STK", "exchange": "SMART",
        "primary_exchange": "NASDAQ", "currency": "USD", "action": "BUY", "quantity": "2",
        "order_type": "MKT", "time_in_force": "DAY", "outside_rth": False,
        "status": "Submitted", "warning": "",
    }]
    coordinator = PythonEventCoordinator(
        journal=journal, versions=versions,
        sandbox=NoProposalSandbox(proposals=([{"side": "BUY", "integer_qty": 2}]
                                             if outbox_link == "current_run" else [])),
    )
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("recovery must not request a send risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("recovery must not use a sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-reopened", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    if outbox_link != "current_run":
        with pytest.raises(ManagedPythonPaperSessionError, match="exact committed outbox link in the current run"):
            session.start_paused()
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert service.lease_held is True
        assert runtime.process.order_sends == []
        assert runtime.stop() is True
        allocator.close()
        return

    if not matching_identity:
        with pytest.raises(ManagedPythonPaperSessionError, match="intent, broker binding, and verified feed differ"):
            session.start_paused()
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert service.lease_held is True
        assert runtime.process.order_sends == []
        assert runtime.stop() is True
        allocator.close()
        return

    snapshot = session.start_paused()
    assert snapshot["reconciled"] is True
    assert service.state()[0] is ExecutionControlState.PAUSED
    assert service.lease_held is True
    assert len(session.broker.orders) == 1
    restored = session.broker.orders[77]
    assert restored.status == bt.Order.Partial
    assert restored.executed.size == 1
    assert restored.executed.price == pytest.approx(101.0)
    assert session.broker.getposition(feed).size == 1
    assert session.broker.getcash() == pytest.approx(9000.0)
    assert runtime.process.order_sends == []

    # Resume a later execution callback only after ref=77 is in the new native registry.
    assert runtime.callback_worker.pause(timeout=1) is True
    runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
        "request_id": 1, "execution_id": "new-exec-2", "order_id": 5,
        "account": "paper-session", "side": "BOT", "shares": "1", "price": "102",
        "time": "20261021  10:59:50", "symbol": "AAPL", "contract_id": 123,
        "security_type": "STK", "currency": "USD",
    }.items()))))
    time.sleep(0.03)
    assert restored.status == bt.Order.Partial
    assert runtime.process.callbacks.qsize() == 1
    runtime.callback_worker.resume()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and restored.status != bt.Order.Completed:
        time.sleep(0.005)

    assert restored.status == bt.Order.Completed, (
        f"status={restored.getstatusname()}, exception={session.exception}, "
        f"worker_failure={runtime.callback_worker.failure}, pump_fatal={runtime.callback_pump._fatal}, "
        f"inbox={runtime.order_index.pending_inbox()}, callbacks={runtime.order_index.pending_callbacks()}, "
        f"broker_queue={session.broker._callback_queue.qsize()}"
    )
    assert restored.ref == 77
    assert restored.executed.size == 2
    assert session.broker.getposition(feed).size == 2
    assert session.broker.getcash() == pytest.approx(8898.0)
    assert ledger.get_order(intent.intent_id).filled_qty == 2
    assert runtime.process.order_sends == []
    assert service.state()[0] is ExecutionControlState.PAUSED

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and runtime.order_index.pending_inbox():
        time.sleep(0.005)
    assert runtime.order_index.pending_inbox() == []

    assert session.stop(timeout=2) is False
    assert session.is_alive is False
    assert "position market value is unavailable" in str(session.exception)
    assert runtime.stop() is True
    allocator.close()


def test_managed_session_restores_partial_buy_before_resuming_python_checkpoint(tmp_path):
    database = tmp_path / "managed-checkpoint-order-reopen.sqlite"
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-checkpoint-order", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint={"event_count": 0, "close_total": "0"},
    )
    first_bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 19),
        start_at=datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    )
    next_bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        timezone="America/New_York", open=Decimal("103"), high=Decimal("103"),
        low=Decimal("103"), close=Decimal("103"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    )
    checkpoint = {"event_count": 1, "close_total": "100", "last_close": "100"}

    # Recreate the exact durable lineage that B127 requires for restoring an
    # acknowledged, partially-filled BUY order: current run event/result,
    # frozen proposal, prepared intent, and broker/native ref binding.
    seed_ledger = SQLiteOrderLedger(database)
    seed_index = BrokerOrderIndex(database)
    intent = seed_ledger.create_intent(OrderIntentInput(
        decision_id="checkpoint-order-event-1", broker="IBKR", account_ref="paper-session",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=version.version_hash, input_event_seq=1, action_index=0,
        bar_end=first_bar.end_at, instrument_id="AAPL", side="BUY", integer_qty=2,
        reference_price=Decimal("100"), eligible_at=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="b" * 64,
    ))
    intent_input = seed_ledger.get_intent(intent.intent_id)
    seed_ledger.reserve(intent.intent_id, "checkpoint-order-reservation", "202")
    seed_ledger.start_send(
        intent.intent_id, attempt_id="checkpoint-order-attempt", client_order_id="checkpoint-client-order",
    )
    seed_index.bind(BrokerOrderBinding(
        broker_order_id=5, account_ref="paper-session", environment=Environment.PAPER,
        intent_id=intent.intent_id, bt_order_ref=77, requested_qty=2, side="BUY",
        contract_id=123, symbol="AAPL", security_type="STK", currency="USD",
    ))
    seed_ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "checkpoint-order-ack",
        "kind": BrokerEventKind.ACK, "broker_order_id": "5",
        "occurred_at": NOW - timedelta(minutes=2), "received_at": NOW - timedelta(minutes=2),
    })
    seed_ledger.record_broker_event({
        "broker": "IBKR", "account_ref": "paper-session", "environment": Environment.PAPER,
        "intent_id": intent.intent_id, "broker_event_id": "checkpoint-order-fill-1",
        "kind": BrokerEventKind.FILL, "broker_order_id": "5", "broker_exec_id": "checkpoint-exec-1",
        "quantity": 1, "price": Decimal("101"),
        "occurred_at": NOW - timedelta(minutes=1), "received_at": NOW - timedelta(minutes=1),
    })
    staged = journal.stage_next_event(
        "run-checkpoint-order", {"bar": first_bar.model_dump(mode="json")},
        event_id="run-checkpoint-order:event-1",
    )
    released = journal.release_next_event("run-checkpoint-order")
    assert released.seq == staged.seq == 1
    journal.commit_result("run-checkpoint-order", {
        "event_id": released.event_id,
        "state": checkpoint,
        "order_proposals": [{"side": "BUY", "integer_qty": 2}],
    })
    frozen = journal.prepare_outbox_intent(
        "run-checkpoint-order", 1, 0,
        intent=intent_input.model_dump(mode="json"), decision={"allowed": True},
    )
    assert frozen["intent"] == intent_input.model_dump(mode="json")
    journal.link_outbox_intent("run-checkpoint-order", 1, 0, intent.intent_id)
    del seed_index, seed_ledger, journal

    # New runtime/session objects reopen the same durable SQLite database.
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    runtime.process.next_valid_id = 6
    runtime.process.snapshot_positions = [{
        "request_id": 1900000001, "account": "paper-session", "contract_id": 123,
        "symbol": "AAPL", "currency": "USD", "quantity": "1", "average_cost": "101",
    }]
    runtime.process.snapshot_open_orders = [{
        "order_id": 5, "account": "paper-session", "permanent_id": 50, "contract_id": 123,
        "symbol": "AAPL", "security_type": "STK", "exchange": "SMART",
        "primary_exchange": "NASDAQ", "currency": "USD", "action": "BUY", "quantity": "2",
        "order_type": "MKT", "time_in_force": "DAY", "outside_rth": False,
        "status": "Submitted", "warning": "",
    }]
    reopened_journal = SQLiteEventJournal(database)
    resumed_sandbox = StatefulCheckpointSandbox(
        proposals=[{"side": "BUY", "integer_qty": 2}],
        proposal_event_ids={"run-checkpoint-order:event-1"},
    )
    coordinator = PythonEventCoordinator(journal=reopened_journal, versions=versions, sandbox=resumed_sandbox)
    handoff = PythonOutboxIntentHandoff(journal=reopened_journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda *_args, **_kwargs: pytest.fail("recovery must not request a send risk snapshot"),
        sender=lambda *_args, **_kwargs: pytest.fail("recovery must not use a sender"),
        now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-checkpoint-order", version_store=versions,
        version_id=version.version_id, journal=reopened_journal, coordinator=coordinator,
        handoff=handoff, dispatch=dispatch, feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    def wait_for(predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        assert predicate(), message

    try:
        snapshot = session.start_paused()
        assert snapshot["reconciled"] is True
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert reopened_journal.get_checkpoint("run-checkpoint-order") == checkpoint
        assert len(resumed_sandbox.requests) == 1, "startup must shadow replay the committed event once"
        assert resumed_sandbox.requests[0]["event_id"] == released.event_id
        assert resumed_sandbox.requests[0]["state"] == {"event_count": 0, "close_total": "0"}
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='run-checkpoint-order'"
            ).fetchone() == (2,)
        assert len(session.broker.orders) == 1
        restored = session.broker.orders[77]
        assert restored.status == bt.Order.Partial
        assert restored.executed.size == 1
        assert session.broker.getposition(feed).size == 1
        assert session.broker.getcash() == pytest.approx(9000.0)
        assert runtime.process.order_sends == []

        # The callback worker durably receives the remaining execution first;
        # Backtrader then consumes it against the restored native ref=77.
        assert runtime.callback_worker.pause(timeout=1) is True
        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 1, "execution_id": "checkpoint-exec-2", "order_id": 5,
            "account": "paper-session", "side": "BOT", "shares": "1", "price": "102",
            "time": "20261021  10:59:50", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))
        assert runtime.process.callbacks.qsize() == 1
        runtime.callback_worker.resume()
        wait_for(lambda: ledger.get_order(intent.intent_id).filled_qty == 2,
                 "callback worker did not persist the remaining durable fill")
        wait_for(lambda: restored.status == bt.Order.Completed,
                 "remaining fill was not routed to restored native order ref=77")
        wait_for(lambda: not index.pending_inbox() and not index.pending_callbacks(),
                 "restored order callback was not durably acknowledged")
        assert restored.ref == 77
        assert restored.executed.size == 2
        assert restored.executed.price == pytest.approx(101.5)
        assert session.broker.getposition(feed).size == 2
        assert session.broker.getcash() == pytest.approx(8898.0)
        assert ledger.get_order(intent.intent_id).filled_qty == 2
        assert runtime.process.order_sends == []

        # Once the restored active order is terminal, the next completed bar
        # may reach the Python worker using the exact persisted checkpoint.
        session.activate()
        assert service.state()[0] is ExecutionControlState.RUNNING
        session.push_completed_bar(next_bar)
        wait_for(lambda: reopened_journal.get_checkpoint("run-checkpoint-order")["event_count"] == 2,
                 "resumed strategy event did not commit")
        assert len(resumed_sandbox.requests) == 2
        assert resumed_sandbox.requests[1]["state"] == checkpoint
        assert resumed_sandbox.requests[1]["event"]["bar"]["session_date"] == "2026-10-20"
        assert reopened_journal.get_checkpoint("run-checkpoint-order") == {
            "event_count": 2, "close_total": "203", "last_close": "103",
        }
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='run-checkpoint-order'"
            ).fetchone() == (3,)
            committed_events = db.execute(
                "SELECT COUNT(*) FROM strategy_event WHERE run_id='run-checkpoint-order' AND committed_at IS NOT NULL"
            ).fetchone()[0]
        assert committed_events == 2, "restart must not create a duplicate committed event"
        assert runtime.process.order_sends == [], "recovery and replay must not resend the restored order"
        assert session.stop(timeout=2) is True
        assert service.state()[0] is ExecutionControlState.STOPPED
    finally:
        if session._thread is not None and session._thread.is_alive():
            for completed_feed in session.feeds.values():
                completed_feed.stop_feed()
            session._thread.join(timeout=2)
        runtime.health_monitor.stop(timeout=1)
        runtime.callback_worker.stop(timeout=1)
        runtime.process.close(timeout=1)
        if service._lease is not None:
            service._lease.close()
            service._lease = None
        allocator.close()


def test_managed_session_resumes_nonempty_python_checkpoint_after_sqlite_reopen(tmp_path):
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    initial_checkpoint = {"event_count": 0, "close_total": "0"}

    def bar(day: int, close: str) -> Bar:
        session_date = date(2026, 10, day)
        start = datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc)
        value = Decimal(close)
        return Bar(
            instrument_id="AAPL", session_date=session_date, start_at=start, end_at=end,
            available_at=end, timezone="America/New_York", open=value, high=value,
            low=value, close=value, volume=Decimal("1000"), currency="USD",
            is_final=True, source="fixture", adjustment="unadjusted",
        )

    first_bar = bar(19, "100")
    next_bar = bar(20, "103")

    def build_session(database, run_id, sandbox):
        runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
        journal = SQLiteEventJournal(database)
        handoff = PythonOutboxIntentHandoff(
            journal=journal, ledger=ledger, authorize=lambda _proposal: None,
        )
        dispatch = PaperOrderDispatchService(
            ledger=ledger,
            facts_provider=lambda *_args, **_kwargs: pytest.fail("no proposal should request send risk facts"),
            sender=lambda *_args, **_kwargs: pytest.fail("managed port owns the transport"),
            now=lambda: NOW,
        )
        feed = PaperCompletedBarFeed(
            instrument_id="AAPL", timezone="America/New_York", queue_size=4,
            idle_poll_seconds=0.01, now=lambda: NOW,
        )
        coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
        session = ManagedPythonPaperSession(
            runtime=runtime, run_id=run_id, version_store=versions, version_id=version.version_id,
            journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
            feeds={"AAPL": feed}, data_by_con_id={123: feed},
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
            queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
        )
        return session, runtime, service, allocator, journal

    def wait_for(predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        assert predicate(), message

    # Build an uninterrupted real Cerebro reference using the same frozen
    # version, initial JSON state, and completed bars.
    reference_db = tmp_path / "uninterrupted.sqlite"
    reference_journal = SQLiteEventJournal(reference_db)
    reference_journal.create_run(
        run_id="reference-run", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint=initial_checkpoint,
    )
    reference_sandbox = StatefulCheckpointSandbox()
    reference, reference_runtime, _reference_service, reference_allocator, _ = build_session(
        reference_db, "reference-run", reference_sandbox,
    )
    reference.start_paused()
    reference.activate()
    reference.push_completed_bar(first_bar)
    wait_for(lambda: reference.journal.get_checkpoint("reference-run")["event_count"] == 1,
             "uninterrupted reference did not commit its first state transition")
    reference.push_completed_bar(next_bar)
    wait_for(lambda: reference.journal.get_checkpoint("reference-run")["event_count"] == 2,
             "uninterrupted reference did not commit its second state transition")
    assert reference.stop(timeout=2) is True
    reference_allocator.close()
    assert reference.strategy is not None
    expected_outcomes = reference.strategy.results
    assert len(expected_outcomes) == 2
    expected_first = expected_outcomes[0].outcome.result
    expected_second = expected_outcomes[1].outcome.result
    assert expected_second.state == {"event_count": 2, "close_total": "203", "last_close": "103"}
    assert reference_runtime.process.order_sends == []

    # Reopen a separate durable run after committing its first event. The
    # second runtime/session must resume from the stored non-empty checkpoint,
    # without replaying the already committed bar into the strategy worker.
    recovery_db = tmp_path / "reopened.sqlite"
    recovery_journal = SQLiteEventJournal(recovery_db)
    recovery_journal.create_run(
        run_id="recovery-run", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint=initial_checkpoint,
    )
    staged = recovery_journal.stage_next_event(
        "recovery-run", {"bar": first_bar.model_dump(mode="json")}, event_id="recovery-event-1",
    )
    released = recovery_journal.release_next_event("recovery-run")
    assert released.seq == staged.seq == 1
    recovery_journal.commit_result("recovery-run", {
        "event_id": released.event_id,
        "state": expected_first.state,
        "order_proposals": [],
    })
    assert recovery_journal.get_checkpoint("recovery-run") == expected_first.state
    del recovery_journal

    resumed_sandbox = StatefulCheckpointSandbox()
    resumed, resumed_runtime, resumed_service, resumed_allocator, reopened_journal = build_session(
        recovery_db, "recovery-run", resumed_sandbox,
    )
    try:
        resumed.start_paused()
        assert reopened_journal.get_checkpoint("recovery-run") == expected_first.state
        assert len(resumed_sandbox.requests) == 1, "startup must shadow replay the committed event once"
        assert resumed_sandbox.requests[0]["event_id"] == released.event_id
        assert resumed_sandbox.requests[0]["state"] == initial_checkpoint
        with sqlite3.connect(recovery_db) as db:
            assert db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='recovery-run'"
            ).fetchone() == (2,)

        resumed.activate()
        resumed.push_completed_bar(next_bar)
        wait_for(lambda: reopened_journal.get_checkpoint("recovery-run")["event_count"] == 2,
                 "resumed session did not commit the next event")
        assert len(resumed_sandbox.requests) == 2
        assert resumed_sandbox.requests[1]["state"] == expected_first.state
        assert reopened_journal.get_checkpoint("recovery-run") == expected_second.state
        with sqlite3.connect(recovery_db) as db:
            assert db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='recovery-run'"
            ).fetchone() == (3,)
            result_json = db.execute(
                "SELECT result_json FROM event_result WHERE run_id='recovery-run' AND seq=2"
            ).fetchone()[0]
        resumed_result = json.loads(result_json)
        assert resumed_result["state"] == expected_second.state
        assert resumed_result["order_proposals"] == [
            proposal.model_dump(mode="json") for proposal in expected_second.order_proposals
        ]
        assert resumed_runtime.process.order_sends == [], "state replay must not create a duplicate broker send"
        assert resumed_service.state()[0] is ExecutionControlState.RUNNING
        assert resumed.stop(timeout=2) is True
        assert resumed_service.state()[0] is ExecutionControlState.STOPPED
    finally:
        if resumed._thread is not None and resumed._thread.is_alive():
            for feed in resumed.feeds.values():
                feed.stop_feed()
            resumed._thread.join(timeout=2)
        resumed_runtime.health_monitor.stop(timeout=1)
        resumed_runtime.callback_worker.stop(timeout=1)
        resumed_runtime.process.close(timeout=1)
        if resumed_service._lease is not None:
            resumed_service._lease.close()
            resumed_service._lease = None
        resumed_allocator.close()


def test_managed_session_rejects_bad_conid_mapping_before_runtime_start(tmp_path):
    database = tmp_path / "managed.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-1", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(ledger=ledger, facts_provider=lambda *_a, **_k: None,
                                         sender=lambda *_a, **_k: None, now=lambda: NOW)
    feed = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    with pytest.raises(ManagedPythonPaperSessionError, match="verified conId/feed mapping"):
        ManagedPythonPaperSession(
            runtime=runtime, run_id="run-1", version_store=versions, version_id=version.version_id,
            journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
            feeds={"AAPL": feed}, data_by_con_id={456: feed},
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        )
    mismatched_feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="UTC", now=lambda: NOW,
    )
    with pytest.raises(ManagedPythonPaperSessionError, match="matching trusted completed-bar feed"):
        ManagedPythonPaperSession(
            runtime=runtime, run_id="run-1", version_store=versions, version_id=version.version_id,
            journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
            feeds={"AAPL": mismatched_feed}, data_by_con_id={123: mismatched_feed},
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        )
    assert runtime._started is False
    assert service.lease_held is False
    allocator.close()


def test_managed_session_refuses_to_attach_native_orders_it_cannot_restore(tmp_path):
    database = tmp_path / "managed.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-1", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=lambda *_a, **_k: None,
        sender=lambda *_a, **_k: None, now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-1", version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
    )
    ledger.create_intent(OrderIntentInput(
        decision_id="prior-decision", broker="IBKR", account_ref="paper-session",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=version.version_hash, input_event_seq=1, action_index=0,
        bar_end=NOW, instrument_id="AAPL", side="BUY", integer_qty=1,
        reference_price=Decimal("100"), eligible_at=NOW, expires_at=NOW + timedelta(minutes=5),
        risk_snapshot_id="a" * 64,
    ))

    with pytest.raises(ManagedPythonPaperSessionError, match="exact committed strategy outbox link"):
        session.start_paused()

    assert service.state()[0] is ExecutionControlState.PAUSED
    assert service.lease_held is True
    assert session.is_alive is False
    assert runtime.stop() is True  # CREATED, never-sent intent is safe to retain while stopping.
    allocator.close()


def test_managed_start_late_failure_persists_pause_and_keeps_owner_lease(tmp_path, monkeypatch):
    database = tmp_path / "managed-start-failure.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-start-failure", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=NoProposalSandbox())
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=lambda *_a, **_k: None,
        sender=lambda *_a, **_k: pytest.fail("startup failure must never send"), now=lambda: NOW,
    )
    feed = PaperCompletedBarFeed(instrument_id="AAPL", now=lambda: NOW)
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-start-failure", version_store=versions,
        version_id=version.version_id, journal=journal, coordinator=coordinator,
        handoff=handoff, dispatch=dispatch, feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
    )

    def fail_after_hydration(_created):
        raise ManagedPythonPaperSessionError("injected late startup failure")

    monkeypatch.setattr(session, "_queue_recovered_created_intents", fail_after_hydration)
    try:
        with pytest.raises(ManagedPythonPaperSessionError, match="injected late startup failure"):
            session.start_paused()

        assert service.state()[0] is ExecutionControlState.PAUSED
        assert service.lease_held is True
        assert runtime.process.order_sends == []
        assert session.is_alive is False
        with pytest.raises(RunUnavailable, match="run is not running"):
            journal.get_run_identity("run-start-failure")
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT state FROM execution_run WHERE run_id = ?", ("run-start-failure",),
            ).fetchone() == ("PAUSED",)
        with pytest.raises(ManagedPythonPaperSessionError, match="not ready for activation"):
            session.activate()
        assert runtime.stop() is True
    finally:
        if runtime._started and service.lease_held:
            runtime.stop()
        allocator.close()


@pytest.mark.parametrize("initially_paused", [False, True])
@pytest.mark.parametrize("reject_second_check", [False, True])
@pytest.mark.parametrize("stop_before_activation", [False, True])
def test_managed_start_stages_committed_outbox_intent_until_explicit_activation(
    tmp_path, initially_paused, reject_second_check, stop_before_activation,
):
    database = tmp_path / "managed-outbox-recovery.sqlite"
    runtime, service, ledger, _index, allocator = _make_runtime(tmp_path, database)
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    journal.create_run(
        run_id="run-outbox-recovery", deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
    )
    bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc), timezone="America/New_York",
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        close=Decimal("100"), volume=Decimal("1000"), currency="USD",
        is_final=True, source="outbox-recovery-fixture", adjustment="unadjusted",
    )
    event = journal.stage_next_event(
        "run-outbox-recovery", {"bar": bar.model_dump(mode="json")}, event_id="outbox-recovery-event",
    )
    journal.release_next_event("run-outbox-recovery")
    journal.commit_result("run-outbox-recovery", {
        "event_id": event.event_id, "state": {"events": 1},
        "order_proposals": [{"side": "BUY", "integer_qty": 2}],
    })
    if initially_paused:
        journal.pause_run("run-outbox-recovery")
    prefix_before = journal.read_committed_prefix("run-outbox-recovery")
    authorize_calls = []

    def authorize(proposal):
        authorize_calls.append(proposal.event_id)
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
            deployment_id="deployment-1", strategy_version_hash=version.version_hash,
            input_event_seq=proposal.seq, action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at, instrument_id="AAPL", side="BUY", integer_qty=2,
            reference_price=Decimal("100"), eligible_at=NOW,
            expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="a" * 64,
        )
        decision = RiskDecision(
            allowed=True, reason_codes=(), checked_at=NOW, risk_snapshot_id="a" * 64,
            estimated_notional=Decimal("200"), estimated_cost=Decimal("200"),
            projected_strategy_exposure=Decimal("200"), projected_gross_exposure=Decimal("200"),
        )
        return AuthorizedProposal(intent, decision, "IBKR", Decimal("100"))

    coordinator = PythonEventCoordinator(
        journal=journal, versions=versions,
        sandbox=NoProposalSandbox([{"side": "BUY", "integer_qty": 2}]),
    )
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)
    grant = RiskGrant.model_validate({
        "grant_id": "grant-outbox-recovery", "user_action_id": "approval-outbox-recovery",
        "strategy_version_hash": version.version_hash, "broker": "IBKR",
        "account_ref": "paper-session", "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "5000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })

    facts_calls = []

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        facts_calls.append(intent.decision_id)
        return DispatchRiskSnapshot(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id=intent.instrument_id, now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=Decimal("100"),
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"),
                available_cash=Decimal("0") if reject_second_check and len(facts_calls) >= 2 else Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("100")),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_sell_reservations=0,
                strategy_daily_loss=Decimal("0"), orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="deployment-1",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process,
        intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
    feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York", now=lambda: NOW)
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id="run-outbox-recovery", version_store=versions,
        version_id=version.version_id, journal=journal, coordinator=coordinator,
        handoff=handoff, dispatch=dispatch, feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        now=lambda: NOW,
    )

    try:
        session.start_paused()

        ready = ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
        ).ready_to_reserve
        assert len(ready) == 1
        intent_id = ready[0].intent_id
        intent = ledger.get_intent(intent_id)
        assert intent.decision_id == "run-outbox-recovery:outbox-recovery-event:0"
        assert ready[0].state is LocalOrderState.CREATED
        assert journal.list_dispatchable_outbox("run-outbox-recovery", include_paused=True) == []
        prefix_after = journal.read_committed_prefix("run-outbox-recovery")
        assert prefix_after == prefix_before
        assert journal.get_checkpoint("run-outbox-recovery") == {"events": 1}
        assert authorize_calls == [event.event_id]
        assert runtime.process.order_sends == []
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert session.is_alive is True
        time.sleep(0.05)
        assert runtime.process.order_sends == []  # PAUSED queue does not create a native order.
        if stop_before_activation:
            assert session.stop() is True
            assert service.state()[0] is ExecutionControlState.STOPPED
            assert service.lease_held is False
            assert runtime.process.order_sends == []
            assert ledger.get_order(intent_id).state is LocalOrderState.CREATED
            assert runtime.callback_worker.alive is False
            assert not any(
                thread.name in {"ibkr-callback-pump", "managed-python-paper-run-outbox-recovery"}
                and thread.is_alive()
                for thread in threading.enumerate()
            )
            with pytest.raises(RunUnavailable, match="run is not running"):
                journal.get_run_identity("run-outbox-recovery")
            assert session.is_alive is False
            return
        session.activate()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not runtime.process.order_sends and session.exception is None:
            time.sleep(0.01)
        if reject_second_check:
            assert runtime.process.order_sends == []
            assert session.exception is not None
            pause_deadline = time.monotonic() + 1
            while time.monotonic() < pause_deadline and service.state()[0] is not ExecutionControlState.PAUSED:
                time.sleep(0.005)
            assert service.state()[0] is ExecutionControlState.PAUSED, f"exception={session.exception!r}"
            assert ledger.get_order(intent_id).state is LocalOrderState.CREATED
            assert len(facts_calls) >= 2
        else:
            assert len(runtime.process.order_sends) == 1, (
                f"exception={session.exception!r}, state={service.state()}, "
                f"queue={list(session.broker._recovered_intents) if session.broker else None}"
            )
            assert runtime.process.order_sends[0]["order"]["quantity"] == 2
            fill_deadline = time.monotonic() + 3
            while time.monotonic() < fill_deadline:
                if (
                    ledger.get_order(intent_id).state is LocalOrderState.FILLED
                    and not runtime.order_index.pending_inbox()
                ):
                    break
                time.sleep(0.01)
            assert ledger.get_order(intent_id).state is LocalOrderState.FILLED
            assert ledger.get_order(intent_id).filled_qty == 2
            assert runtime.order_index.pending_inbox() == []
        assert authorize_calls == [event.event_id]
    finally:
        if session.is_alive:
            session.stop()
        if runtime._started and service.lease_held:
            runtime.stop()
        lingering = [
            (thread.name, thread.is_alive()) for thread in threading.enumerate()
            if thread.name in {"ibkr-callback-pump", "managed-python-paper-run-outbox-recovery"}
        ]
        assert not lingering, (
            f"lingering={lingering}, runtime_started={runtime._started}, "
            f"worker={runtime.callback_worker.alive}, service={service.state()}, "
            f"session_thread={session._thread.is_alive() if session._thread else None}"
        )
        assert not any(
            thread.name in {"ibkr-callback-pump", "managed-python-paper-run-outbox-recovery"}
            and thread.is_alive()
            for thread in threading.enumerate()
        )
        allocator.close()


@pytest.mark.parametrize("reject_second_check", [False, True])
@pytest.mark.parametrize("terminal_update", ["fill", "cancel", "reject"])
def test_failed_recovery_sends_exact_created_intent_before_retrying_released_bar(
    tmp_path, reject_second_check, terminal_update,
):
    database = tmp_path / "managed-created-with-retry.sqlite"
    runtime, service, ledger, index, allocator = _make_runtime(tmp_path, database)
    runtime.process.auto_fill_on_send = False
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(database)
    run_id = "run-created-with-retry"
    journal.create_run(
        run_id=run_id, deployment_id="deployment-1", account_ref="paper-session",
        environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        initial_checkpoint={"event_count": 0, "close_total": "0"},
    )
    prior_bar = Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 19),
        start_at=datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="created-recovery-fixture", adjustment="unadjusted",
    )
    retry_bar = prior_bar.model_copy(update={
        "session_date": date(2026, 10, 20),
        "start_at": datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        "end_at": datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        "available_at": datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        "source": "created-recovery-retry-fixture",
    })
    prior_event = journal.stage_next_event(
        run_id, {"bar": prior_bar.model_dump(mode="json")}, event_id=f"{run_id}:event-1",
    )
    journal.release_next_event(run_id)
    journal.commit_result(run_id, {
        "event_id": prior_event.event_id,
        "state": {"event_count": 1, "close_total": "100", "last_close": "100"},
        "order_proposals": [{"side": "BUY", "integer_qty": 2}],
    })
    retry_event = journal.stage_next_event(
        run_id, {"bar": retry_bar.model_dump(mode="json")}, event_id=f"{run_id}:event-2",
    )
    journal.release_next_event(run_id)

    intent = ledger.create_intent(OrderIntentInput(
        decision_id=f"{run_id}:{prior_event.event_id}:0", broker="IBKR",
        account_ref="paper-session", environment=Environment.PAPER,
        deployment_id="deployment-1", strategy_version_hash=version.version_hash,
        input_event_seq=1, action_index=0, bar_end=prior_bar.end_at,
        instrument_id="AAPL", side="BUY", integer_qty=2,
        reference_price=Decimal("100"), eligible_at=NOW,
        expires_at=NOW + timedelta(minutes=30), risk_snapshot_id="f" * 64,
    ))
    intent_input = ledger.get_intent(intent.intent_id)
    journal.prepare_outbox_intent(
        run_id, 1, 0, intent=intent_input.model_dump(mode="json"), decision={"allowed": True},
    )
    journal.link_outbox_intent(run_id, 1, 0, intent.intent_id)

    sandbox = StatefulCheckpointSandbox(
        proposals=[{"side": "BUY", "integer_qty": 2}],
        proposal_event_ids={prior_event.event_id},
    )
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=lambda _proposal: None)
    grant = RiskGrant.model_validate({
        "grant_id": "grant-created-retry", "user_action_id": "approval-created-retry",
        "strategy_version_hash": version.version_hash, "broker": "IBKR",
        "account_ref": "paper-session", "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "5000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })

    def facts_provider(current_intent, *, excluding_intent_id):
        assert current_intent.decision_id == intent_input.decision_id
        assert excluding_intent_id == intent.intent_id
        return DispatchRiskSnapshot(
            grant=grant,
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
                quote_as_of=NOW, account_as_of=NOW, quote_price=Decimal("100"),
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("10000"),
                available_cash=Decimal("0") if reject_second_check else Decimal("9000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("100")),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_sell_reservations=0,
                strategy_daily_loss=Decimal("0"), orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="deployment-1",
        )

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *_args, **_kwargs: pytest.fail("managed port must supply its trusted sender"),
        now=lambda: NOW,
    )
    runtime.paper_transport = IbkrSubprocessPaperTransport(
        process=runtime.process, intent_loader=ledger.get_intent,
        contract_loader=lambda _instrument: {
            "con_id": 123, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
    )
    runtime.cancel_transport = IbkrSubprocessCancelTransport(process=runtime.process)
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
        idle_poll_seconds=0.01, now=lambda: NOW,
    )
    session = ManagedPythonPaperSession(
        runtime=runtime, run_id=run_id, version_store=versions, version_id=version.version_id,
        journal=journal, coordinator=coordinator, handoff=handoff, dispatch=dispatch,
        feeds={"AAPL": feed}, data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        queue_size=4, idle_poll_seconds=0.01, now=lambda: NOW,
    )

    def wait_for(predicate, message):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        assert predicate(), message

    def checkpoint():
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT checkpoint_json FROM execution_run WHERE run_id=?", (run_id,),
            ).fetchone()
        return json.loads(row[0])

    try:
        session.start_paused()
        assert session.broker._recovered_intents
        assert runtime.process.order_sends == []

        # Fail while the exact released retry event and prior CREATED intent
        # coexist; both durable identities must survive the failed session.
        service.pause(reason="injected_created_retry_failure")
        feed.push_completed_bar(retry_bar)
        wait_for(lambda: session.exception is not None, "paused strategy did not fail closed")
        wait_for(lambda: session._thread is not None and not session._thread.is_alive(),
                 "failed Cerebro thread did not exit")
        failed_event = journal.inspect_paused_released_event(run_id)
        assert failed_event.event_id == retry_event.event_id
        assert failed_event.seq == 2
        assert ledger.get_order(intent.intent_id).state is LocalOrderState.CREATED
        assert runtime.process.order_sends == []

        runtime.snapshot_id_factory = lambda: 1900000002
        session.recover_failed_session()
        assert service.state()[0] is ExecutionControlState.PAUSED
        assert journal.inspect_paused_released_event(run_id) == failed_event
        assert session._recovery_created_intent_id == intent.intent_id
        assert runtime.process.order_sends == []
        assert len(sandbox.requests) == 2  # committed prefix shadow replay only

        session.activate()
        wait_for(
            lambda: len(runtime.process.order_sends) == 1 or session.exception is not None,
            "recovered CREATED order remained queued without a send or failure",
        )
        if reject_second_check:
            assert session.exception is not None
            wait_for(
                lambda: service.state()[0] is ExecutionControlState.PAUSED,
                "second-risk denial did not pause the runtime",
            )
            assert runtime.process.order_sends == []
            assert ledger.get_order(intent.intent_id).state is LocalOrderState.CREATED
            assert journal.inspect_paused_released_event(run_id) == failed_event
            assert checkpoint() == {"event_count": 1, "close_total": "100", "last_close": "100"}
            assert len(sandbox.requests) == 2
            return
        assert session.exception is None, (
            f"recovery exception={session.exception!r}, state={service.state()}, "
            f"queue={list(session.broker._recovered_intents) if session.broker else None}, "
            f"thread_alive={session.is_alive}"
        )
        assert len(runtime.process.order_sends) == 1
        assert ledger.get_order(intent.intent_id).state is LocalOrderState.SEND_STARTED
        assert journal.inspect_paused_released_event(run_id) == failed_event
        assert checkpoint() == {"event_count": 1, "close_total": "100", "last_close": "100"}
        assert len(sandbox.requests) == 2, "retry bar must wait for a terminal result on the prior intent"

        if terminal_update in {"cancel", "reject"}:
            sent = runtime.process.order_sends[0]
            if terminal_update == "cancel":
                runtime.process.callbacks.put(SimpleNamespace(kind="order_status", payload=tuple(sorted({
                    "request_id": 4, "order_id": sent["broker_order_id"],
                    "status": "Cancelled", "filled": "0", "remaining": "2",
                }.items()))))
                terminal_state = LocalOrderState.CANCELLED
            else:
                runtime.process.callbacks.put(SimpleNamespace(kind="error", payload=tuple(sorted({
                    "request_id": sent["broker_order_id"], "error_code": 201,
                    "error_string": "injected broker rejection",
                }.items()))))
                terminal_state = LocalOrderState.REJECTED
            wait_for(lambda: ledger.get_order(intent.intent_id).state is terminal_state,
                     f"broker {terminal_update} did not reach the recovered intent ledger")
            wait_for(lambda: session.exception is not None,
                     "canceled recovery intent did not force fresh reconciliation")
            wait_for(lambda: service.state()[0] is ExecutionControlState.PAUSED,
                     "canceled recovery intent did not pause the service")
            assert journal.inspect_paused_released_event(run_id) == failed_event
            assert checkpoint() == {"event_count": 1, "close_total": "100", "last_close": "100"}
            assert len(sandbox.requests) == 2
            return

        # Only a broker fill makes the prior intent terminal. The broker-cycle
        # hook then resumes the exact event and the frozen worker processes it.
        sent = runtime.process.order_sends[0]
        runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 4, "execution_id": "created-retry-fill",
            "order_id": sent["broker_order_id"], "account": "paper-session",
            "side": "BOT", "shares": "2", "price": "101",
            "time": "20261021  11:00:00", "symbol": "AAPL",
            "contract_id": 123, "security_type": "STK", "currency": "USD",
        }.items()))))
        wait_for(lambda: ledger.get_order(intent.intent_id).state is LocalOrderState.FILLED,
                 "terminal fill did not reach the durable ledger")
        wait_for(lambda: checkpoint() == {
            "event_count": 2, "close_total": "200", "last_close": "100",
        }, "released retry bar did not run after the prior order became terminal")
        assert len(sandbox.requests) == 3
        assert sandbox.requests[0]["event_id"] == prior_event.event_id
        assert sandbox.requests[1]["event_id"] == prior_event.event_id
        assert sandbox.requests[2]["event_id"] == failed_event.event_id
        assert ledger.get_order(intent.intent_id).state is LocalOrderState.FILLED
        assert len(runtime.process.order_sends) == 1
        assert journal.read_committed_prefix(run_id).events[-1].event_id == failed_event.event_id
        assert session.stop(timeout=2) is True
        assert runtime.stop() is True
    finally:
        current = ledger.get_order(intent.intent_id)
        if runtime.process.order_sends and current.state not in {
            LocalOrderState.FILLED, LocalOrderState.CANCELLED, LocalOrderState.REJECTED,
        } and runtime.callback_worker.alive:
            sent = runtime.process.order_sends[0]
            runtime.process.callbacks.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
                "request_id": 5, "execution_id": "created-retry-cleanup-fill",
                "order_id": sent["broker_order_id"], "account": "paper-session",
                "side": "BOT", "shares": "2", "price": "101",
                "time": "20261021  11:00:01", "symbol": "AAPL",
                "contract_id": 123, "security_type": "STK", "currency": "USD",
            }.items()))))
            wait_for(
                lambda: ledger.get_order(intent.intent_id).state is LocalOrderState.FILLED,
                "cleanup callback could not settle recovered order",
            )
        if session.is_alive:
            session.stop(timeout=2)
        if service.lease_held:
            runtime_stopped = runtime.stop()
            if not runtime_stopped:
                # Failed Cerebro deliberately leaves callback deliveries
                # unacknowledged for recovery. The production runtime keeps
                # its callback pump and owner lease alive in STOPPING;
                # after proving the inbox is durable, terminate this fake
                # test process's worker so pytest can exit cleanly.
                assert service.state()[0] is ExecutionControlState.STOPPING
                assert runtime.process.closed
                assert not runtime.order_index.pending_callbacks()
                assert runtime.order_index.pending_inbox()
                assert runtime.callback_worker.stop(timeout=2)
        leaked_execution_threads = [
            thread.name for thread in threading.enumerate()
            if thread.is_alive() and thread.name in {
                "ibkr-callback-pump",
                f"managed-python-paper-recovery-{run_id}",
            }
        ]
        assert not leaked_execution_threads, leaked_execution_threads
        allocator.close()
