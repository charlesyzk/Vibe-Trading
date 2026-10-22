from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing
import os
import signal
from threading import Barrier, Lock
from time import monotonic, sleep

import pytest

from src.execution.contracts import Environment, RiskGrant
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.order_ledger import (
    BrokerEventKind,
    ExecutionControlState,
    InvalidTransition,
    LocalOrderState,
    SQLiteOrderLedger,
)
from src.execution.risk import RiskFacts


NOW = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
VERSION = "a" * 64


def _grant(**changes):
    values = {
        "grant_id": "grant-1", "user_action_id": "user-action-1",
        "strategy_version_hash": VERSION, "broker": "FAKE", "account_ref": "paper-1",
        "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "1000", "max_gross_exposure": "5000",
        "strategy_budget": "2000", "max_daily_loss": "500", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(hours=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    }
    values.update(changes)
    return RiskGrant.model_validate(values)


def _facts(**changes):
    values = {
        "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
        "strategy_version_hash": VERSION, "instrument_id": "AAPL", "now": NOW,
        "quote_as_of": NOW - timedelta(seconds=1), "account_as_of": NOW - timedelta(seconds=2),
        "quote_price": Decimal("100"), "price_buffer_fraction": Decimal("0.01"),
        "estimated_fees": Decimal("2"), "net_liquidation": Decimal("10000"),
        "available_cash": Decimal("5000"), "positions": (), "strategy_position_qty": 0,
        "strategy_buy_reservations": Decimal("0"), "account_buy_reservations": Decimal("0"),
        "strategy_daily_loss": Decimal("0"), "orders_this_session": 0,
        "account_snapshot_complete": True,
    }
    values.update(changes)
    return RiskFacts.model_validate(values)


def _intent(**changes):
    values = {
        "decision_id": "run-1:event-1:0", "broker": "FAKE", "account_ref": "paper-1",
        "environment": Environment.PAPER, "deployment_id": "deployment-1",
        "strategy_version_hash": VERSION, "input_event_seq": 1, "action_index": 0,
        "bar_end": NOW - timedelta(minutes=1), "instrument_id": "AAPL", "side": "BUY",
        "integer_qty": 2, "order_type": "market", "time_in_force": "DAY",
        "reference_price": Decimal("100"), "eligible_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(minutes=5), "risk_snapshot_id": "b" * 64,
    }
    values.update(changes)
    return values


def _service(ledger, sender, snapshots):
    ledger.set_execution_control(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    calls = []

    def facts_provider(intent, *, excluding_intent_id):
        calls.append((intent.decision_id, excluding_intent_id))
        grant, facts = snapshots[min(len(calls) - 1, len(snapshots) - 1)]
        return DispatchRiskSnapshot(grant=grant, facts=facts, deployment_id="deployment-1")

    return PaperOrderDispatchService(ledger=ledger, facts_provider=facts_provider, sender=sender, now=lambda: NOW), calls


def _activate(ledger, *, broker="FAKE", account_ref="paper-1", environment=Environment.PAPER):
    ledger.set_execution_control(
        broker=broker, account_ref=account_ref, environment=environment,
        state=ExecutionControlState.RUNNING,
    )


def _dispatch_and_pause_after_durable_send_start(database_path, intent_id, committed_marker):
    """Child-process target for a crash between SEND_STARTED and transport entry."""
    from pathlib import Path
    from threading import Event

    class PauseAfterCommitLedger(SQLiteOrderLedger):
        def start_send_after_risk(self, current_intent_id, **kwargs):
            result = super().start_send_after_risk(current_intent_id, **kwargs)
            Path(committed_marker).write_text("committed", encoding="utf-8")
            Event().wait()
            return result

    ledger = PauseAfterCommitLedger(database_path)
    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda intent, *, excluding_intent_id: DispatchRiskSnapshot(
            grant=_grant(), facts=_facts(), deployment_id="deployment-1",
        ),
        sender=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transport must not be entered before the crash")
        ),
        now=lambda: NOW,
    )
    service.dispatch(intent_id)


def _dispatch_and_pause_after_fake_broker_accept(database_path, intent_id, accepted_log):
    """Child target for broker acceptance followed by lost local acknowledgement."""
    from pathlib import Path
    from threading import Event

    def sender(intent, *, attempt_id, client_order_id):
        with Path(accepted_log).open("a", encoding="utf-8") as stream:
            stream.write(f"{attempt_id}\t{client_order_id}\n")
            stream.flush()
            os.fsync(stream.fileno())
        Event().wait()

    ledger = SQLiteOrderLedger(database_path)
    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda intent, *, excluding_intent_id: DispatchRiskSnapshot(
            grant=_grant(), facts=_facts(), deployment_id="deployment-1",
        ),
        sender=sender,
        now=lambda: NOW,
    )
    service.dispatch(intent_id)


def test_dispatch_rechecks_after_reservation_and_persists_send_boundary(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    sends = []
    service, fact_calls = _service(ledger, lambda intent, **ids: sends.append((intent, ids)), [(_grant(), _facts())] * 2)

    result = service.dispatch(record.intent_id)

    assert result.status == "SEND_STARTED"
    assert len(fact_calls) == 2
    assert fact_calls == [("run-1:event-1:0", record.intent_id)] * 2
    assert len(sends) == 1
    assert ledger.get_order(record.intent_id).state is LocalOrderState.SEND_STARTED
    assert ledger.get_order(record.intent_id).reserved_amount == Decimal("204.00")


def test_fresh_scope_starts_paused_and_dispatch_does_not_load_risk_facts(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    sends = []
    fact_calls = []

    def facts_provider(intent, *, excluding_intent_id):
        fact_calls.append(intent.intent_id)
        return DispatchRiskSnapshot(grant=_grant(), facts=_facts(), deployment_id="deployment-1")

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=lambda *args, **kwargs: sends.append(args),
        now=lambda: NOW,
    )

    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("execution_paused",)
    assert fact_calls == []
    assert sends == []
    assert ledger.get_order(record.intent_id).state is LocalOrderState.CREATED


def test_pause_between_reservation_and_send_is_checked_inside_send_transaction(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    _activate(ledger)
    calls = 0
    sends = []

    def facts_provider(intent, *, excluding_intent_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            ledger.set_execution_control(
                broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
                state=ExecutionControlState.PAUSED, reason="operator_pause",
            )
        return DispatchRiskSnapshot(grant=_grant(), facts=_facts(), deployment_id="deployment-1")

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=lambda *args, **kwargs: sends.append(args),
        now=lambda: NOW,
    )

    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("execution_not_running",)
    assert sends == []
    current = ledger.get_order(record.intent_id)
    assert current.state is LocalOrderState.CREATED
    assert current.reserved_amount == 0
    assert ledger.get_execution_control(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER
    ) == (ExecutionControlState.PAUSED, "operator_pause")


@pytest.mark.parametrize(
    "second_snapshot,expected_reason",
    [
        ((_grant(revoked_at=NOW), _facts()), "grant_revoked"),
        ((_grant(), _facts(quote_as_of=NOW - timedelta(minutes=1))), "quote_stale_or_future"),
        ((_grant(), _facts(available_cash=Decimal("100"))), "available_cash"),
    ],
)
def test_second_risk_failure_releases_hold_and_never_calls_sender(tmp_path, second_snapshot, expected_reason):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    sends = []
    service, _ = _service(ledger, lambda *args, **kwargs: sends.append(args), [(_grant(), _facts()), second_snapshot])

    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.intent_id == record.intent_id
    assert expected_reason in result.reason_codes
    assert sends == []
    current = ledger.get_order(record.intent_id)
    assert current.state is LocalOrderState.CREATED
    assert current.reserved_amount == 0
    assert ledger.recovery_snapshot().ready_to_reserve == (current,)


def test_uncertain_sender_is_unknown_and_never_retried(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    sends = []

    def timeout(intent, **ids):
        sends.append(ids)
        raise OSError("transport outcome uncertain")

    service, _ = _service(ledger, timeout, [(_grant(), _facts())] * 2)
    with pytest.raises(OSError, match="outcome uncertain"):
        service.dispatch(record.intent_id)

    retry = service.dispatch(record.intent_id)
    assert retry.status == "RECONCILE"
    assert len(sends) == 1
    assert ledger.get_order(record.intent_id).state is LocalOrderState.UNKNOWN


def test_start_send_failure_prevents_sender_call(tmp_path):
    class BrokenStartLedger(SQLiteOrderLedger):
        def start_send_after_risk(self, intent_id, **kwargs):
            raise InvalidTransition("fixture commit failure")

    ledger = BrokenStartLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent())
    sends = []
    service, _ = _service(ledger, lambda *args, **kwargs: sends.append(args), [(_grant(), _facts())] * 2)

    with pytest.raises(InvalidTransition, match="commit failure"):
        service.dispatch(record.intent_id)

    assert sends == []
    assert ledger.get_order(record.intent_id).state is LocalOrderState.CREATED
    assert ledger.get_order(record.intent_id).reserved_amount == 0


def test_process_crash_after_send_started_commit_never_retries_transport(tmp_path):
    database_path = tmp_path / "crash-after-send-start.sqlite"
    committed_marker = tmp_path / "send-start-committed"
    ledger = SQLiteOrderLedger(database_path)
    _activate(ledger)
    record = ledger.create_intent(_intent())

    process = multiprocessing.get_context("fork").Process(
        target=_dispatch_and_pause_after_durable_send_start,
        args=(str(database_path), record.intent_id, str(committed_marker)),
    )
    process.start()
    try:
        deadline = monotonic() + 5
        while not committed_marker.exists() and process.is_alive() and monotonic() < deadline:
            sleep(0.01)
        assert committed_marker.exists(), "child did not commit SEND_STARTED"
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    recovered = SQLiteOrderLedger(database_path)
    assert recovered.get_order(record.intent_id).state is LocalOrderState.SEND_STARTED
    sends = []
    service, fact_calls = _service(
        recovered,
        lambda *args, **kwargs: sends.append((args, kwargs)),
        [(_grant(), _facts())] * 2,
    )

    retry = service.dispatch(record.intent_id)

    assert retry.status == "RECONCILE"
    assert retry.reason_codes == ("send_outcome_unresolved",)
    assert sends == []
    assert fact_calls == []
    assert recovered.get_order(record.intent_id).state is LocalOrderState.SEND_STARTED


def test_process_crash_after_fake_broker_accept_before_ack_does_not_duplicate(tmp_path):
    database_path = tmp_path / "accepted-before-ack.sqlite"
    accepted_log = tmp_path / "fake-broker-accepted.log"
    ledger = SQLiteOrderLedger(database_path)
    _activate(ledger)
    record = ledger.create_intent(_intent())
    suffix = hashlib.sha256(record.intent_id.encode("utf-8")).hexdigest()[:32]
    expected_attempt_id = f"attempt:{suffix}"
    expected_client_order_id = f"client:{suffix}"

    process = multiprocessing.get_context("fork").Process(
        target=_dispatch_and_pause_after_fake_broker_accept,
        args=(str(database_path), record.intent_id, str(accepted_log)),
    )
    process.start()
    try:
        deadline = monotonic() + 5
        while not accepted_log.exists() and process.is_alive() and monotonic() < deadline:
            sleep(0.01)
        assert accepted_log.exists(), "fake broker did not durably accept the request"
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    recovered = SQLiteOrderLedger(database_path)
    sends = []

    def retry_sender(intent, *, attempt_id, client_order_id):
        sends.append((attempt_id, client_order_id))
        with accepted_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{attempt_id}\t{client_order_id}\n")

    service, fact_calls = _service(
        recovered, retry_sender, [(_grant(), _facts())] * 2,
    )
    retry = service.dispatch(record.intent_id)

    assert accepted_log.read_text(encoding="utf-8").splitlines() == [
        f"{expected_attempt_id}\t{expected_client_order_id}"
    ]
    assert retry.status == "RECONCILE"
    assert retry.reason_codes == ("send_outcome_unresolved",)
    assert sends == []
    assert fact_calls == []
    assert recovered.get_order(record.intent_id).state is LocalOrderState.SEND_STARTED


def test_risk_provider_failure_after_reservation_releases_hold(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent())
    calls = 0
    sends = []

    def facts_provider(intent, *, excluding_intent_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture account snapshot unavailable")
        return DispatchRiskSnapshot(grant=_grant(), facts=_facts(), deployment_id="deployment-1")

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=lambda *args, **kwargs: sends.append(args),
        now=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        service.dispatch(record.intent_id)

    assert sends == []
    current = ledger.get_order(record.intent_id)
    assert current.state is LocalOrderState.CREATED
    assert current.reserved_amount == 0


def test_deployment_owner_mismatch_never_reaches_sender(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent())
    snapshots = iter(("deployment-1", "other-deployment"))
    sends = []

    def facts_provider(intent, *, excluding_intent_id):
        return DispatchRiskSnapshot(
            grant=_grant(),
            facts=_facts(),
            deployment_id=next(snapshots),
        )

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=lambda *args, **kwargs: sends.append(args),
        now=lambda: NOW,
    )
    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("deployment_mismatch",)
    assert sends == []
    assert ledger.get_order(record.intent_id).state is LocalOrderState.CREATED


def test_intent_expiring_after_snapshot_is_rejected_inside_send_transaction(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent())
    times = iter((NOW, NOW, NOW + timedelta(minutes=6)))
    sends = []
    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda intent, **kwargs: DispatchRiskSnapshot(
            grant=_grant(), facts=_facts(), deployment_id="deployment-1"
        ),
        sender=lambda *args, **kwargs: sends.append(args),
        now=lambda: next(times),
    )

    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("intent_expired",)
    assert sends == []
    assert ledger.get_order(record.intent_id).state is LocalOrderState.CREATED


def test_ledger_send_gate_requires_share_reservation_for_sell(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent(side="SELL"))

    with pytest.raises(InvalidTransition, match="share reservation"):
        ledger.reserve(record.intent_id, "reservation-1", "204")
    assert ledger.get_order(record.intent_id).state is LocalOrderState.CREATED


def test_late_fill_after_cancel_blocks_new_scope_dispatch(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    cancelled = ledger.create_intent(_intent(decision_id="cancelled-run-order"))
    ledger.reserve(cancelled.intent_id, "reservation-old", "202")
    ledger.start_send(cancelled.intent_id, attempt_id="attempt-old", client_order_id="client-old")
    ledger.record_broker_event({
        "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
        "intent_id": cancelled.intent_id, "broker_event_id": "ack-old",
        "kind": BrokerEventKind.ACK, "broker_order_id": "broker-old",
        "occurred_at": NOW, "received_at": NOW,
    })
    ledger.record_broker_event({
        "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
        "intent_id": cancelled.intent_id, "broker_event_id": "cancel-old",
        "kind": BrokerEventKind.CANCELLED, "broker_order_id": "broker-old",
        "occurred_at": NOW, "received_at": NOW,
    })
    ledger.record_broker_event({
        "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
        "intent_id": cancelled.intent_id, "broker_event_id": "late-fill-old",
        "kind": BrokerEventKind.FILL, "broker_exec_id": "execution-old",
        "broker_order_id": "broker-old", "quantity": 1, "price": Decimal("100"),
        "occurred_at": NOW, "received_at": NOW,
    })

    next_order = ledger.create_intent(_intent(decision_id="following-order"))
    ledger.reserve(next_order.intent_id, "reservation-new", "202")
    current, decision = ledger.start_send_after_risk(
        next_order.intent_id,
        attempt_id="attempt-new",
        client_order_id="client-new",
        grant=_grant(),
        facts=_facts(),
        deployment_id="deployment-1",
        now=lambda: NOW,
    )

    assert decision.allowed is False
    assert decision.reason_codes == ("execution_not_running",)
    assert current.state is LocalOrderState.CREATED
    assert current.reserved_amount == 0
    assert current.attempt_id is None


def test_ledger_send_gate_rejects_wrong_bound_account_even_when_grant_and_facts_match(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    _activate(ledger)
    record = ledger.create_intent(_intent())
    ledger.reserve(record.intent_id, "reservation-1", "204")
    wrong_grant = _grant(account_ref="other-account")
    wrong_facts = _facts(account_ref="other-account")

    current, decision = ledger.start_send_after_risk(
        record.intent_id,
        attempt_id="attempt-1",
        client_order_id="client-1",
        grant=wrong_grant,
        facts=wrong_facts,
        deployment_id="deployment-1",
        now=lambda: NOW,
    )

    assert decision.allowed is False
    assert "account_mismatch" in decision.reason_codes
    assert current.state is LocalOrderState.CREATED


def test_non_paper_intent_is_rejected_before_risk_or_sender(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent(environment=Environment.LIVE))
    sends = []
    service, fact_calls = _service(ledger, lambda *args, **kwargs: sends.append(args), [(_grant(), _facts())])

    result = service.dispatch(record.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("paper_only",)
    assert fact_calls == []
    assert sends == []


def test_sell_dispatch_reserves_owned_shares_and_starts_one_send(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent(side="SELL", integer_qty=7))
    sends = []
    service, fact_calls = _service(
        ledger, lambda *args, **kwargs: sends.append(args),
        [(_grant(), _facts(strategy_position_qty=10))] * 2,
    )

    result = service.dispatch(record.intent_id)

    assert result.status == "SEND_STARTED"
    assert len(fact_calls) == 2
    assert len(sends) == 1
    assert ledger.get_order(record.intent_id).reserved_amount == 0
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 7


def test_sell_risk_adds_external_and_local_share_holds(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "sell-holds.sqlite")
    existing = ledger.create_intent(_intent(side="SELL", integer_qty=3, decision_id="existing-sell"))
    ledger.reserve_sell(
        existing.intent_id, "existing-share-hold", available_position_qty=10,
        account_as_of=NOW - timedelta(seconds=2),
    )
    requested = ledger.create_intent(_intent(side="SELL", integer_qty=6, decision_id="new-sell"))
    sends = []
    service, fact_calls = _service(
        ledger, lambda *args, **kwargs: sends.append(args),
        [(_grant(), _facts(strategy_position_qty=10, strategy_sell_reservations=2))],
    )

    result = service.dispatch(requested.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("sell_exceeds_strategy_position",)
    assert len(fact_calls) == 1
    assert sends == []
    assert ledger.get_order(requested.intent_id).state is LocalOrderState.CREATED


def test_final_sell_risk_rechecks_external_share_holds_before_send(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "final-sell-holds.sqlite")
    requested = ledger.create_intent(_intent(side="SELL", integer_qty=7))
    sends = []
    service, fact_calls = _service(
        ledger, lambda *args, **kwargs: sends.append(args),
        [
            (_grant(), _facts(strategy_position_qty=10)),
            (_grant(), _facts(strategy_position_qty=10, strategy_sell_reservations=4)),
        ],
    )

    result = service.dispatch(requested.intent_id)

    assert result.status == "REJECTED"
    assert result.reason_codes == ("sell_exceeds_strategy_position",)
    assert len(fact_calls) == 2
    assert sends == []
    assert ledger.get_order(requested.intent_id).state is LocalOrderState.CREATED
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 0


def test_concurrent_sell_intents_cannot_reserve_more_than_strategy_position(tmp_path):
    path = tmp_path / "concurrent-sells.sqlite"
    first_ledger = SQLiteOrderLedger(path)
    second_ledger = SQLiteOrderLedger(path)
    records = [
        first_ledger.create_intent(_intent(side="SELL", integer_qty=7, decision_id="sell-one")),
        second_ledger.create_intent(_intent(side="SELL", integer_qty=7, decision_id="sell-two")),
    ]
    _activate(first_ledger)
    barrier = Barrier(2)
    calls_by_intent = {record.intent_id: 0 for record in records}
    call_lock = Lock()
    sends = []
    send_lock = Lock()

    def provider(intent, *, excluding_intent_id):
        with call_lock:
            calls_by_intent[excluding_intent_id] += 1
            call_number = calls_by_intent[excluding_intent_id]
        if call_number == 1:
            barrier.wait(timeout=3)
        return DispatchRiskSnapshot(
            grant=_grant(), facts=_facts(strategy_position_qty=10), deployment_id="deployment-1",
        )

    services = [
        PaperOrderDispatchService(
            ledger=ledger, facts_provider=provider,
            sender=lambda intent, **ids: append_send(intent, ids),
            now=lambda: NOW,
        )
        for ledger in (first_ledger, second_ledger)
    ]

    def append_send(intent, ids):
        with send_lock:
            sends.append((intent.decision_id, ids))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].dispatch(pair[1].intent_id), zip(services, records)))

    assert [result.status for result in results].count("SEND_STARTED") == 1
    assert len(sends) == 1
    assert first_ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 7


def test_sell_partial_fill_cancel_race_updates_share_reservation_and_late_fill_pauses(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "sell-lifecycle.sqlite")
    record = ledger.create_intent(_intent(side="SELL", integer_qty=7))
    service, _ = _service(
        ledger, lambda *args, **kwargs: None,
        [(_grant(), _facts(strategy_position_qty=10))] * 2,
    )
    assert service.dispatch(record.intent_id).status == "SEND_STARTED"
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 7

    def event(event_id, kind, **facts):
        return ledger.record_broker_event({
            "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
            "intent_id": record.intent_id, "broker_event_id": event_id, "kind": kind,
            "broker_order_id": "44", "occurred_at": NOW, "received_at": NOW, **facts,
        })

    event("sell-ack", BrokerEventKind.ACK)
    event("sell-fill-1", BrokerEventKind.FILL, broker_exec_id="sell-exec-1", quantity=3, price=Decimal("101"))
    assert not event(
        "sell-fill-1", BrokerEventKind.FILL,
        broker_exec_id="sell-exec-1", quantity=3, price=Decimal("101"),
    )
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 4
    assert not ledger.sell_position_snapshot_is_current(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL", account_as_of=NOW - timedelta(seconds=1),
    )
    assert not ledger.sell_position_snapshot_is_current(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL", account_as_of=NOW,
    )

    event("sell-cancel-pending", BrokerEventKind.CANCEL_PENDING)
    event("sell-cancel-rejected", BrokerEventKind.CANCEL_REJECTED)
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 4
    event("sell-cancelled", BrokerEventKind.CANCELLED)
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 0
    event("sell-late-fill", BrokerEventKind.FILL, broker_exec_id="sell-exec-late", quantity=1, price=Decimal("100"))
    assert ledger.get_execution_control(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
    )[0] is ExecutionControlState.RECOVERING


def test_sell_broker_rejection_releases_share_reservation(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "rejected-sell.sqlite")
    record = ledger.create_intent(_intent(side="SELL", integer_qty=3))
    service, _ = _service(
        ledger, lambda *args, **kwargs: None,
        [(_grant(), _facts(strategy_position_qty=3))] * 2,
    )
    assert service.dispatch(record.intent_id).status == "SEND_STARTED"

    ledger.record_broker_event({
        "broker": "FAKE", "account_ref": "paper-1", "environment": Environment.PAPER,
        "intent_id": record.intent_id, "broker_event_id": "sell-rejected",
        "kind": BrokerEventKind.REJECT, "broker_order_id": "44",
        "occurred_at": NOW, "received_at": NOW,
    })

    assert ledger.get_order(record.intent_id).state is LocalOrderState.REJECTED
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 0


def test_uncertain_sell_send_retains_all_unfilled_share_reservation(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "unknown-sell.sqlite")
    record = ledger.create_intent(_intent(side="SELL", integer_qty=4))

    def uncertain(*args, **kwargs):
        raise OSError("unknown send outcome")

    service, _ = _service(
        ledger, uncertain, [(_grant(), _facts(strategy_position_qty=4))] * 2,
    )
    with pytest.raises(OSError, match="unknown send outcome"):
        service.dispatch(record.intent_id)
    assert ledger.get_order(record.intent_id).state is LocalOrderState.UNKNOWN
    assert ledger.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 4
    recovered = SQLiteOrderLedger(tmp_path / "unknown-sell.sqlite")
    assert recovered.reserved_sell_quantity(
        broker="FAKE", account_ref="paper-1", environment=Environment.PAPER,
        instrument_id="AAPL",
    ) == 4
    assert recovered.get_order(record.intent_id).state is LocalOrderState.UNKNOWN


def test_concurrent_dispatch_of_same_intent_has_one_sender_call(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    record = ledger.create_intent(_intent())
    barrier = Barrier(2)
    send_lock = Lock()
    sends = []

    def facts_provider(intent, *, excluding_intent_id):
        barrier.wait(timeout=3)
        return DispatchRiskSnapshot(grant=_grant(), facts=_facts(), deployment_id="deployment-1")

    def sender(intent, **ids):
        with send_lock:
            sends.append(ids)

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=sender,
        now=lambda: NOW,
    )
    _activate(ledger)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.dispatch(record.intent_id), range(2)))

    assert sorted(result.status for result in results) == ["RECONCILE", "SEND_STARTED"]
    assert len(sends) == 1
    assert ledger.get_order(record.intent_id).state is LocalOrderState.SEND_STARTED


def test_ledger_reservations_prevent_two_intents_from_spending_same_cash(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    first = ledger.create_intent(_intent(decision_id="run-1:event-1:0"))
    second = ledger.create_intent(_intent(decision_id="run-1:event-2:0", input_event_seq=2))
    sends = []
    service, _ = _service(
        ledger,
        lambda intent, **ids: sends.append(intent.decision_id),
        [(_grant(), _facts(available_cash=Decimal("300")))],
    )

    first_result = service.dispatch(first.intent_id)
    second_result = service.dispatch(second.intent_id)

    assert first_result.status == "SEND_STARTED"
    assert second_result.status == "REJECTED"
    assert "available_cash" in second_result.reason_codes
    assert sends == ["run-1:event-1:0"]
    assert ledger.get_order(second.intent_id).state is LocalOrderState.CREATED


def test_concurrent_over_budget_intents_never_both_reach_sender(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    first = ledger.create_intent(_intent(decision_id="run-1:event-1:0"))
    second = ledger.create_intent(_intent(decision_id="run-1:event-2:0", input_event_seq=2))
    second_check_barrier = Barrier(2)
    sends = []
    send_lock = Lock()
    provider_calls = {}
    provider_lock = Lock()

    def facts_provider(intent, *, excluding_intent_id):
        with provider_lock:
            count = provider_calls.get(intent.decision_id, 0) + 1
            provider_calls[intent.decision_id] = count
        if count == 2:
            second_check_barrier.wait(timeout=3)
        return DispatchRiskSnapshot(
            grant=_grant(),
            facts=_facts(available_cash=Decimal("300")),
            deployment_id="deployment-1",
        )

    def sender(intent, **ids):
        with send_lock:
            sends.append(intent.decision_id)

    service = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=facts_provider,
        sender=sender,
        now=lambda: NOW,
    )
    _activate(ledger)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item_id: service.dispatch(item_id), (first.intent_id, second.intent_id)))

    assert sum(result.status == "SEND_STARTED" for result in results) <= 1
    assert len(sends) <= 1
