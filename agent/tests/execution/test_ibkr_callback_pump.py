from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
import time
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.execution.contracts import Environment
from src.execution.ibkr_callback_pump import (
    BrokerOrderBinding,
    BrokerOrderIndex,
    CallbackPumpFatal,
    IbkrCallbackPump,
    IbkrCallbackPumpWorker,
)
from src.execution.order_ledger import (
    BrokerEventKind,
    ExecutionControlState,
    LocalOrderState,
    OrderIntentInput,
    SQLiteOrderLedger,
)


NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
VERSION = "a" * 64
RISK = "b" * 64


def _event(kind: str, **payload):
    return SimpleNamespace(kind=kind, payload=tuple(sorted(payload.items())))


def _setup(tmp_path, *, sink=None, reconciliation_sink=None):
    db = tmp_path / "callback-runtime.sqlite"
    ledger = SQLiteOrderLedger(db)
    index = BrokerOrderIndex(db)
    order = ledger.create_intent(OrderIntentInput(
        decision_id="decision-1", broker="IBKR", account_ref="paper-fixture",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=VERSION, input_event_seq=1, action_index=0,
        bar_end=NOW, instrument_id="XYZ", side="BUY", integer_qty=10,
        reference_price=Decimal("12.50"), eligible_at=NOW,
        expires_at=NOW.replace(day=21), risk_snapshot_id=RISK,
    ))
    ledger.reserve(order.intent_id, "reserve-1", "125.00")
    ledger.start_send(order.intent_id, attempt_id="attempt-1", client_order_id="client-1")
    binding = BrokerOrderBinding(
        broker_order_id=41, account_ref="paper-fixture", environment=Environment.PAPER,
        intent_id=order.intent_id, bt_order_ref=7, requested_qty=10, side="BUY",
        contract_id=123, symbol="XYZ", security_type="STK", currency="USD",
    )
    events: Queue = Queue(maxsize=8)
    overflowed = Event()
    delivered = []
    pump = IbkrCallbackPump(
        events=events, overflowed=overflowed, ledger=ledger, order_index=index,
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        callback_sink=(sink or delivered.append), now=lambda: NOW,
        reconciliation_sink=reconciliation_sink,
    )
    pump.bind_order(binding)
    return ledger, index, events, overflowed, delivered, pump, order


def _execution(*, exec_id="exec-1", request_id=1, account="paper-fixture", shares="4", price="12.5", side="BOT"):
    return _event(
        "execution", request_id=request_id, execution_id=exec_id, order_id=41,
        account=account, side=side, shares=shares, price=price,
        time="20261020  11:00:00", symbol="XYZ", contract_id=123,
        security_type="STK", currency="USD",
    )


def test_callback_is_committed_to_ledger_before_cumulative_delivery(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_execution())

    result = pump.process_one()

    current = ledger.get_order(order.intent_id)
    assert result.disposition == "APPLIED"
    assert current.state is LocalOrderState.PARTIALLY_FILLED
    assert current.filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert delivered[-1].order_ref == 7
    assert delivered[-1].filled_qty == 4
    assert delivered[-1].average_fill_price == 12.5


def test_callback_worker_pause_waits_for_inflight_processing_and_resume_restarts_it(tmp_path):
    _, _, _, _, _, pump, _ = _setup(tmp_path)
    processing = Event()
    release = Event()
    processed = []

    def controlled_process_one():
        processed.append("started")
        if len(processed) == 1:
            processing.set()
            assert release.wait(1)
        return SimpleNamespace(disposition="EMPTY")

    pump.process_one = controlled_process_one
    worker = IbkrCallbackPumpWorker(pump, poll_interval=0.001)
    worker.start()
    assert processing.wait(1)

    pause_result = []
    paused = Event()
    pauser = Thread(target=lambda: (pause_result.append(worker.pause(timeout=1)), paused.set()))
    pauser.start()
    time.sleep(0.02)
    assert not paused.is_set(), "pause must wait until the in-flight process_one call exits"
    release.set()
    assert paused.wait(1)
    pauser.join(1)
    assert pause_result == [True]

    count_at_pause = len(processed)
    time.sleep(0.02)
    assert len(processed) == count_at_pause

    worker.resume()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and len(processed) == count_at_pause:
        time.sleep(0.001)
    assert len(processed) > count_at_pause
    assert worker.stop(timeout=1)


def test_paused_callback_worker_can_rebind_and_replay_unacked_delivery(tmp_path):
    ledger, index, events, _, old_sink, pump, order = _setup(tmp_path)
    pump.defer_delivery_ack = True
    events.put(_execution())
    assert pump.process_one().disposition == "APPLIED"
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert len(index.pending_inbox()) == 1
    assert len(old_sink) == 1
    expected_ids = index.pending_delivery_event_ids()
    assert expected_ids == {old_sink[0].event_id}

    worker = IbkrCallbackPumpWorker(pump, poll_interval=0.001)
    worker.start()
    new_sink = []
    with pytest.raises(RuntimeError, match="must be paused"):
        worker.rebind_delivery_sink(new_sink.append, expected_pending_event_ids=expected_ids)
    assert worker.pause(timeout=1)
    with pytest.raises(CallbackPumpFatal, match="identities changed"):
        worker.rebind_delivery_sink(new_sink.append, expected_pending_event_ids=set())
    assert index.pending_delivery_event_ids() == expected_ids

    worker.rebind_delivery_sink(new_sink.append, expected_pending_event_ids=expected_ids)
    assert index.pending_delivery_event_ids() == expected_ids
    worker.resume()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not new_sink:
        time.sleep(0.001)
    assert len(new_sink) == 1
    assert new_sink[0] == old_sink[0]
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1

    pump.acknowledge_delivery(new_sink[0].event_id)
    assert worker.pause(timeout=1)
    assert index.pending_inbox() == []
    assert worker.stop(timeout=1)


def test_commission_facts_replay_dedupe_revise_and_link_after_execution(tmp_path):
    ledger, _, events, _, _, pump, order = _setup(tmp_path)
    report = _event("commission", execution_id="exec-1", commission="0.35",
                    currency="usd", realized_pnl="0", yield_value="0")

    events.put(report)
    first = pump.process_one()
    events.put(report)
    duplicate = pump.process_one()
    assert first.disposition == "FEE_RECORDED"
    assert duplicate.disposition == "FEE_DUPLICATE"
    assert duplicate.event_id == first.event_id
    facts = ledger.list_fee_facts(broker="IBKR", account_ref="paper-fixture",
                                  environment=Environment.PAPER, broker_exec_id="exec-1")
    assert len(facts) == 1
    assert facts[0]["amount"] == "0.35"
    assert facts[0]["association_status"] == "PENDING_EXECUTION"
    assert facts[0]["review_status"] == "NO_REVISION_SEEN"
    assert facts[0]["amount_status"] == "REPORTED_UNCONFIRMED"

    # A changed broker report is evidence, not an in-place correction.
    events.put(_event("commission", execution_id="exec-1", commission="0.40",
                      currency="USD", realized_pnl="0", yield_value="0"))
    revision = pump.process_one()
    assert revision.disposition == "FEE_PENDING_REVIEW"
    facts = ledger.list_fee_facts(broker_exec_id="exec-1")
    assert [fact["amount"] for fact in facts] == ["0.35", "0.40"]
    assert all(fact["review_status"] == "PENDING_REVIEW" for fact in facts)

    events.put(_execution(exec_id="exec-1"))
    pump.process_one()
    facts = ledger.list_fee_facts(broker_exec_id="exec-1")
    assert all(fact["association_status"] == "LINKED" for fact in facts)
    assert all(fact["intent_id"] == order.intent_id for fact in facts)

    reopened = SQLiteOrderLedger(ledger.path)
    replayed = reopened.list_fee_facts(broker_exec_id="exec-1")
    assert [fact["fee_fact_id"] for fact in replayed] == [fact["fee_fact_id"] for fact in facts]
    assert [fact["amount"] for fact in replayed] == ["0.35", "0.40"]


@pytest.mark.parametrize("payload", [
    {"execution_id": "", "commission": "0.1", "currency": "USD"},
    {"execution_id": "exec-1", "commission": "NaN", "currency": "USD"},
    {"execution_id": "exec-1", "commission": "0.1", "currency": ""},
    {"execution_id": "exec-1", "commission": "0.1", "currency": "USD", "account": "other"},
])
def test_invalid_commission_fact_fails_closed_without_persisting(tmp_path, payload):
    ledger, _, events, _, _, pump, _ = _setup(tmp_path)
    events.put(_event("commission", **payload))
    with pytest.raises(CallbackPumpFatal):
        pump.process_one()
    assert ledger.list_fee_facts() == []


@pytest.mark.parametrize("error_code", [2104, 2106, 2107, 2108, 2158])
def test_documented_farm_status_notifications_are_acknowledged_without_pausing(tmp_path, error_code):
    _, _, events, _, _, pump, _ = _setup(tmp_path)
    events.put(_event("error", request_id=-1, error_code=error_code, message="status notice"))

    result = pump.process_one()

    assert result.disposition == "IGNORED"
    assert pump._fatal is None


@pytest.mark.parametrize("error_code", [1100, 1101, 1102, 1300, 999999])
def test_connectivity_and_unknown_errors_pause_for_reconciliation(tmp_path, error_code):
    _, _, events, _, _, pump, _ = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    events.put(_event("error", request_id=41, error_code=error_code, message="fixture"))

    with pytest.raises(CallbackPumpFatal, match="IBKR"):
        pump.process_one()

    assert latch.is_set()
    if error_code in {1100, 1101, 1102, 1300}:
        assert "reconciliation" in str(pump._fatal)
    else:
        assert "unclassified" in str(pump._fatal)


def test_bound_order_rejection_is_durably_applied_to_ledger_and_native_callback(tmp_path):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    payload = {
        "request_id": 41,
        "error_time": 1730000000,
        "error_code": 201,
        "message": "Order rejected",
        "advanced_reject": '{"reason":"fixture"}',
    }
    events.put(_event("error", **payload))

    first = pump.process_one()
    events.put(_event("error", **payload))
    replay = pump.process_one()

    current = ledger.get_order(order.intent_id)
    assert first.disposition == "APPLIED"
    assert replay.disposition == "DUPLICATE"
    assert current.state is LocalOrderState.REJECTED
    assert current.filled_qty == 0
    assert current.reserved_amount == Decimal("0")
    assert ledger.list_fills(order.intent_id) == []
    assert delivered[-1].status.value == "REJECTED"
    assert len(delivered) == 1
    assert index.resolve(41).intent_id == order.intent_id


def test_unbound_order_rejection_pauses_without_changing_any_order(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    events.put(_event("error", request_id=99, error_time=1730000000,
                      error_code=201, message="Order rejected", advanced_reject=""))

    with pytest.raises(CallbackPumpFatal, match="unmapped broker order id"):
        pump.process_one()

    assert latch.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    assert delivered == []


def test_order_rejection_after_partial_fill_pauses_for_reconciliation(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    events.put(_execution(shares="3"))
    assert pump.process_one().disposition == "APPLIED"
    events.put(_event("error", request_id=41, error_time=1730000000,
                      error_code=201, message="Order rejected", advanced_reject=""))

    with pytest.raises(CallbackPumpFatal, match="conflicts with fills"):
        pump.process_one()

    assert latch.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.PARTIALLY_FILLED
    assert ledger.get_order(order.intent_id).filled_qty == 3
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert delivered[-1].status.value == "PARTIALLY_FILLED"


def test_order_rejection_with_wrong_scope_binding_pauses_without_ledger_transition(tmp_path):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    with sqlite3.connect(index.path) as db:
        db.execute(
            "UPDATE broker_order_binding SET account_ref='other-paper-account' WHERE broker_order_id=41"
        )
        db.commit()
    latch = Event()
    pump.pause_latch = latch
    events.put(_event("error", request_id=41, error_time=1730000000,
                      error_code=201, message="Order rejected", advanced_reject=""))

    with pytest.raises(CallbackPumpFatal, match="outside configured account/environment"):
        pump.process_one()

    assert latch.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    assert delivered == []


def test_distinct_rejection_after_terminal_reject_pauses_for_review(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("error", request_id=41, error_time=1730000000,
                      error_code=201, message="Order rejected", advanced_reject="first"))
    assert pump.process_one().disposition == "APPLIED"
    latch = Event()
    pump.pause_latch = latch
    events.put(_event("error", request_id=41, error_time=1730000001,
                      error_code=201, message="Order rejected with changed details",
                      advanced_reject="second"))

    with pytest.raises(CallbackPumpFatal, match="conflicts with fills or current order state"):
        pump.process_one()

    assert latch.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.REJECTED
    assert len(ledger.list_fills(order.intent_id)) == 0
    assert len(delivered) == 1


def test_deferred_delivery_is_queued_once_until_ack_and_replays_after_restart(tmp_path):
    ledger, index, events, overflowed, delivered, pump, order = _setup(tmp_path)
    pump.defer_delivery_ack = True
    events.put(_execution())

    first = pump.process_one()
    waiting = pump.process_one()
    assert first.disposition == "APPLIED"
    assert waiting.disposition == "EMPTY"
    assert len(delivered) == 1
    assert len(index.pending_inbox()) == 1

    # A new pump models process recovery. The durable unacknowledged inbox
    # must replay even though the prior process had already queued it.
    recovered_callbacks = []
    recovered = IbkrCallbackPump(
        events=Queue(maxsize=8), overflowed=overflowed,
        ledger=SQLiteOrderLedger(ledger.path), order_index=BrokerOrderIndex(index.path),
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        callback_sink=recovered_callbacks.append, now=lambda: NOW, defer_delivery_ack=True,
    )
    replay = recovered.process_one()
    assert replay.disposition == "APPLIED"
    assert len(recovered_callbacks) == 1
    assert recovered_callbacks[0].event_id == delivered[0].event_id

    recovered.acknowledge_delivery(recovered_callbacks[0].event_id)
    assert index.pending_inbox() == []
    assert recovered.process_one().disposition == "EMPTY"


def test_deferred_delivery_does_not_block_later_broker_events(tmp_path):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    pump.defer_delivery_ack = True
    events.put(_execution(exec_id="exec-1", shares="4"))
    events.put(_execution(exec_id="exec-2", shares="2", price="13.0"))

    first = pump.process_one()
    second = pump.process_one()

    assert first.disposition == second.disposition == "APPLIED"
    assert [callback.filled_qty for callback in delivered] == [4, 6]
    assert len(index.pending_inbox()) == 2
    assert pump.process_one().disposition == "EMPTY"
    for callback in delivered:
        pump.acknowledge_delivery(callback.event_id)
    assert index.pending_inbox() == []
    assert ledger.get_order(order.intent_id).filled_qty == 6


@pytest.mark.parametrize("identity", [
    {"contract_id": 124, "symbol": "XYZ", "security_type": "STK", "currency": "USD"},
    {"contract_id": 123, "symbol": "ABC", "security_type": "STK", "currency": "USD"},
    {"contract_id": 123, "symbol": "XYZ", "security_type": "OPT", "currency": "USD"},
    {"contract_id": 123, "symbol": "XYZ", "security_type": "STK", "currency": "CAD"},
])
def test_execution_contract_identity_mismatch_fails_before_fill_ledger(tmp_path, identity):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    payload = dict(_execution().payload)
    payload.update(identity)
    events.put(_event("execution", **payload))

    with pytest.raises(CallbackPumpFatal, match="contract identity"):
        pump.process_one()

    current = ledger.get_order(order.intent_id)
    assert current.state is LocalOrderState.SEND_STARTED
    assert current.filled_qty == 0
    assert ledger.list_fills(order.intent_id) == []
    assert delivered == []


def test_open_order_contract_identity_mismatch_latches_pump(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event(
        "open_order", order_id=41, contract_id=999, symbol="XYZ",
        security_type="STK", currency="USD",
    ))

    with pytest.raises(CallbackPumpFatal, match="contract identity"):
        pump.process_one()

    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    assert ledger.list_fills(order.intent_id) == []
    assert delivered == []


def test_next_valid_id_is_forwarded_to_durable_allocator_hook(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    observed = []
    pump.next_valid_id_sink = observed.append
    events.put(_event("next_valid_id", order_id=51))

    result = pump.process_one()

    assert result.disposition == "IGNORED"
    assert observed == [51]
    assert pump.next_valid_id_seen.is_set()
    assert not pump.managed_accounts_seen.is_set()
    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


def test_invalid_next_valid_id_latches_pump_fail_closed(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("next_valid_id", order_id=0))

    with pytest.raises(CallbackPumpFatal, match="order_id must be a positive integer"):
        pump.process_one()

    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


def test_next_valid_id_without_allocator_wiring_fails_closed(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("next_valid_id", order_id=51))

    with pytest.raises(CallbackPumpFatal, match="without a durable order-id allocator"):
        pump.process_one()

    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


@pytest.mark.parametrize("accounts", [
    "other-account", "paper-fixture, other-account", "paper-fixture, paper-fixture",
])
def test_managed_accounts_callback_must_exactly_match_configured_single_account(tmp_path, accounts):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    callback = _event("managed_accounts", accounts=accounts)
    payload = dict(callback.payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_json = json.dumps({"kind": callback.kind, "payload": payload}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    callback_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(index.path) as db:
        db.execute(
            "INSERT INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
            (callback_hash, callback.kind, payload_json),
        )
    with pytest.raises(CallbackPumpFatal, match="does not exactly match"):
        pump.process_one()

    assert index.pending_callbacks()[0][1].kind == "managed_accounts"
    assert not pump.managed_accounts_seen.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


def test_managed_accounts_callback_rejects_empty_account_entries(tmp_path):
    _, _, events, _, _, pump, _ = _setup(tmp_path)
    events.put(_event("managed_accounts", accounts="paper-fixture,"))
    with pytest.raises(CallbackPumpFatal, match="empty account entry"):
        pump.process_one()
    assert not pump.managed_accounts_seen.is_set()


def test_managed_accounts_callback_accepts_only_exact_configured_account(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("managed_accounts", accounts="paper-fixture"))

    result = pump.process_one()

    assert result.disposition == "IGNORED"
    assert pump.managed_accounts_seen.is_set()
    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


def test_account_snapshot_callback_requires_and_uses_trusted_snapshot_consumer(tmp_path):
    _, _, events, _, _, pump, _ = _setup(tmp_path)
    events.put(_event(
        "account_summary", snapshot_id=1900000001, request_id=1900000001,
        account="paper-fixture", tag="NetLiquidation", value="10000", currency="USD",
    ))
    with pytest.raises(CallbackPumpFatal, match="no trusted snapshot consumer"):
        pump.process_one()

    consumed = []
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _, _, events, _, _, pump, _ = _setup(
        trusted, reconciliation_sink=lambda kind, payload: consumed.append((kind, payload)),
    )
    events.put(_event(
        "account_summary", snapshot_id=1900000001, request_id=1900000001,
        account="paper-fixture", tag="NetLiquidation", value="10000", currency="USD",
    ))
    result = pump.process_one()
    assert result.disposition == "IGNORED"
    assert consumed[0][0] == "account_summary"
    assert consumed[0][1]["account"] == "paper-fixture"

def test_background_pump_consumes_shared_spool_without_cerebro_bar(tmp_path):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    observed = []
    pump.next_valid_id_sink = observed.append
    callback = _event("next_valid_id", order_id=51)
    payload = dict(callback.payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_json = json.dumps({"kind": callback.kind, "payload": payload}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    callback_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(index.path) as db:
        db.execute(
            "INSERT INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
            (callback_hash, callback.kind, payload_json),
        )

    worker = IbkrCallbackPumpWorker(pump, poll_interval=0.005)
    worker.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not observed:
        time.sleep(0.005)
    try:
        assert observed == [51]
        with sqlite3.connect(index.path) as db:
            assert db.execute(
                "SELECT processed FROM ibkr_callback_spool WHERE callback_hash=?",
                (callback_hash,),
            ).fetchone() == (1,)
        assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
        assert delivered == []
    finally:
        assert worker.stop(timeout=1)


def test_background_pump_latches_bad_managed_account_and_does_not_ack(tmp_path):
    ledger, index, events, _, delivered, pump, order = _setup(tmp_path)
    callback = _event("managed_accounts", accounts="unrelated-account")
    payload = dict(callback.payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_json = json.dumps({"kind": callback.kind, "payload": payload}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    callback_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(index.path) as db:
        db.execute(
            "INSERT INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
            (callback_hash, callback.kind, payload_json),
        )

    pause_reasons = []
    worker = IbkrCallbackPumpWorker(
        pump, poll_interval=0.005,
        failure_sink=lambda error: pause_reasons.append(str(error)),
    )
    worker.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and worker.failure is None:
        time.sleep(0.005)
    try:
        assert isinstance(worker.failure, CallbackPumpFatal)
        assert "does not exactly match" in str(worker.failure)
        assert pause_reasons and "does not exactly match" in pause_reasons[0]
        assert index.pending_callbacks()[0][1].kind == "managed_accounts"
        assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
        assert delivered == []
    finally:
        assert worker.stop(timeout=1)


def test_duplicate_exec_id_in_different_callback_envelopes_does_not_double_fill(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_execution())
    pump.process_one()
    first_callback = delivered[-1]
    events.put(_execution(exec_id="exec-1", request_id=99))
    pump.process_one()

    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert delivered[-1].event_id == first_callback.event_id


def test_conflicting_exec_id_reuse_latches_pause(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_execution())
    pump.process_one()
    events.put(_execution(exec_id="exec-1", request_id=2, shares="5"))

    with pytest.raises(CallbackPumpFatal, match="execution ID was reused"):
        pump.process_one()

    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert len(delivered) == 1


@pytest.mark.parametrize(
    "callback, message",
    [
        (_execution(account="other-paper"), "account does not match"),
        (_event("execution", request_id=1, execution_id="exec-x", order_id=99,
                account="paper-fixture", side="BOT", shares="1", price="12", time="20261020  11:00:00"),
         "unmapped broker order"),
        (_execution(shares="1.5"), "whole share"),
        (_execution(price="NaN"), "finite"),
        (_execution(side="SLD"), "side does not match"),
    ],
)
def test_bad_callback_latches_pause_and_never_notifies(tmp_path, callback, message):
    ledger, _, events, overflowed, delivered, pump, order = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    events.put(callback)

    with pytest.raises(CallbackPumpFatal, match=message):
        pump.process_one()

    assert latch.is_set()
    assert delivered == []
    assert ledger.get_order(order.intent_id).filled_qty == 0


def test_unknown_or_out_of_scope_order_status_is_fail_closed(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("order_status", order_id=41, status="Inactive", filled="0", remaining="10"))

    with pytest.raises(CallbackPumpFatal, match="unsupported IBKR order status"):
        pump.process_one()

    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED


def test_filled_status_without_matching_execution_facts_latches_pause(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    events.put(_event(
        "order_status", order_id=41, status="Filled", filled="10", remaining="0",
        average_fill_price="12.5",
    ))

    with pytest.raises(CallbackPumpFatal, match="Filled status lacks matching execution details"):
        pump.process_one()

    assert latch.is_set()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    assert ledger.get_order(order.intent_id).filled_qty == 0
    assert delivered == []


def test_filled_status_is_accepted_after_ledger_reconciles_all_executions(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_execution(exec_id="exec-a", shares="6"))
    pump.process_one()
    events.put(_execution(exec_id="exec-b", shares="4"))
    pump.process_one()
    events.put(_event(
        "order_status", order_id=41, status="Filled", filled="10", remaining="0",
        average_fill_price="12.5",
    ))

    assert pump.process_one().disposition == "APPLIED"
    assert ledger.get_order(order.intent_id).state is LocalOrderState.FILLED
    assert ledger.get_order(order.intent_id).filled_qty == 10
    assert len(ledger.list_fills(order.intent_id)) == 2
    assert delivered[-1].status == "FILLED"


def test_binding_must_match_durable_send_intent_before_it_is_indexed(tmp_path):
    ledger, index, events, overflowed, delivered, pump, order = _setup(tmp_path)
    bad_binding = BrokerOrderBinding(
        broker_order_id=42, account_ref="paper-fixture", environment=Environment.PAPER,
        intent_id=order.intent_id, bt_order_ref=8, requested_qty=9, side="BUY",
        contract_id=123, symbol="XYZ", security_type="STK", currency="USD",
    )

    with pytest.raises(CallbackPumpFatal, match="authorized intent"):
        pump.bind_order(bad_binding)

    with pytest.raises(CallbackPumpFatal, match="unmapped broker order"):
        index.resolve(42)


def test_partial_cancel_then_late_fill_remains_monotone(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    ledger.set_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    events.put(_execution(exec_id="exec-1", shares="3"))
    pump.process_one()
    events.put(_event("order_status", order_id=41, status="PendingCancel", filled="3", remaining="7"))
    pump.process_one()
    events.put(_event("order_status", order_id=41, status="Cancelled", filled="3", remaining="7"))
    pump.process_one()
    events.put(_execution(exec_id="exec-late", shares="2", price="13.0"))
    pump.process_one()

    current = ledger.get_order(order.intent_id)
    assert current.state is LocalOrderState.CANCELLED
    assert current.filled_qty == 5
    assert current.remaining_qty == 5
    assert len(ledger.list_fills(order.intent_id)) == 2
    assert delivered[-1].status == "CANCELLED"
    assert delivered[-1].filled_qty == 5
    assert ledger.get_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
    ) == (ExecutionControlState.RECOVERING, "late_fill_after_terminal_order")


def test_inbox_replays_after_ledger_commit_and_sink_failure(tmp_path):
    attempts = 0
    def flaky_sink(callback):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated crash after durable ledger apply")

    ledger, index, events, overflowed, delivered, pump, order = _setup(tmp_path, sink=flaky_sink)
    callback = _execution()
    events.put(callback)
    with pytest.raises(CallbackPumpFatal, match="delivery failed"):
        pump.process_one()
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(index.pending_inbox()) == 1

    replayed = []
    recovered = IbkrCallbackPump(
        events=events, overflowed=overflowed, ledger=SQLiteOrderLedger(ledger.path),
        order_index=BrokerOrderIndex(index.path), broker="IBKR", account_ref="paper-fixture",
        environment=Environment.PAPER, callback_sink=replayed.append, now=lambda: NOW,
    )
    recovered.process_one()
    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert len(replayed) == 1
    assert index.pending_inbox() == []


def test_callback_spool_survives_crash_before_in_memory_queueing(tmp_path):
    ledger, index, events, overflowed, delivered, pump, order = _setup(tmp_path)
    pump.record_callback(_execution())
    assert events.empty()  # Adapter may crash after the durable hook, before put_nowait.

    recovered = IbkrCallbackPump(
        events=Queue(maxsize=8), overflowed=Event(), ledger=SQLiteOrderLedger(ledger.path),
        order_index=BrokerOrderIndex(index.path), broker="IBKR", account_ref="paper-fixture",
        environment=Environment.PAPER, callback_sink=delivered.append, now=lambda: NOW,
    )
    recovered.process_one()

    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert delivered[-1].filled_qty == 4


def test_durable_spool_commission_is_committed_before_marking_processed(tmp_path):
    ledger, index, events, _, _, pump, _ = _setup(tmp_path)
    callback = _event("commission", execution_id="late-exec", commission="1.25", currency="USD")
    payload = dict(callback.payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_json = json.dumps({"kind": callback.kind, "payload": payload}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    callback_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(index.path) as db:
        db.execute(
            "INSERT INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
            (callback_hash, callback.kind, payload_json),
        )

    result = pump.process_one()

    assert result.disposition == "FEE_RECORDED"
    assert index.pending_callbacks() == []
    assert events.empty()
    fee = ledger.list_fee_facts(broker_exec_id="late-exec")
    assert len(fee) == 1
    assert fee[0]["association_status"] == "PENDING_EXECUTION"


def test_fee_fact_rows_cannot_be_overwritten_or_deleted(tmp_path):
    ledger, _, events, _, _, pump, _ = _setup(tmp_path)
    events.put(_event("commission", execution_id="exec-immutable", commission="0.5", currency="USD"))
    pump.process_one()
    with sqlite3.connect(ledger.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE fee_fact SET amount='0.01'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM fee_fact")


def test_pump_consumes_shared_durable_sdk_spool_table(tmp_path):
    ledger, index, events, overflowed, delivered, pump, order = _setup(tmp_path)
    callback = _execution()
    payload = dict(callback.payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body_json = json.dumps({"kind": callback.kind, "payload": payload}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    callback_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(index.path) as db:
        db.execute(
            "INSERT INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
            (callback_hash, callback.kind, payload_json),
        )

    pump.process_one()

    assert ledger.get_order(order.intent_id).filled_qty == 4
    assert len(ledger.list_fills(order.intent_id)) == 1
    assert delivered[-1].filled_qty == 4
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT processed FROM ibkr_callback_spool WHERE callback_hash=?",
                          (callback_hash,)).fetchone() == (1,)


def test_delayed_ack_does_not_clear_cancel_pending(tmp_path):
    ledger, _, events, _, delivered, pump, order = _setup(tmp_path)
    events.put(_event("order_status", order_id=41, status="Submitted", filled="0", remaining="10"))
    pump.process_one()
    events.put(_event("order_status", order_id=41, status="PendingCancel", filled="0", remaining="10"))
    pump.process_one()
    events.put(_event("order_status", order_id=41, status="PreSubmitted", filled="0", remaining="10"))
    pump.process_one()

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CANCEL_PENDING
    assert delivered[-1].status == "ACCEPTED"  # BT has no native pending-cancel status.


def test_queue_overflow_latches_pause_before_processing(tmp_path):
    ledger, _, events, overflowed, delivered, pump, order = _setup(tmp_path)
    latch = Event()
    pump.pause_latch = latch
    overflowed.set()

    with pytest.raises(CallbackPumpFatal, match="queue overflowed"):
        pump.process_one()

    assert latch.is_set()
    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
