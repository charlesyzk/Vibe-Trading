from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from queue import Queue
from threading import Event
import time
from types import SimpleNamespace

import backtrader as bt
import pytest

from src.bt_runtime.brokers.proxy import (
    BrokerCommandUncertain, BrokerOrderCommand, PortBroker, PortOrderRejected,
)
from src.execution.backtrader_dispatch import BacktraderDispatchPort, OneShotLedgerIntentResolver
from src.execution.contracts import Environment, RiskGrant
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.ibkr_callback_pump import (
    BrokerOrderIndex, CallbackPumpFatal, ContractIdentity, IbkrCallbackPump, IbkrCallbackPumpWorker,
)
from src.execution.order_ledger import (
    BrokerEvent,
    BrokerEventKind,
    ExecutionControlState,
    LocalOrderState,
    OrderIntentInput,
    SQLiteOrderLedger,
)
from src.execution.owner_lease import ExecutionOwnerLease
from src.execution.risk import RiskFacts


NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
VERSION = "a" * 64


def _event(kind, **payload):
    return SimpleNamespace(kind=kind, payload=tuple(sorted(payload.items())))


class TwoBarFeed(bt.feed.DataBase):
    def __init__(self):
        super().__init__()
        self.index = 0

    def _load(self):
        if self.index >= 2:
            return False
        self.index += 1
        self.lines.datetime[0] = bt.date2num(datetime(2026, 10, 20 + self.index, 9, 30))
        self.lines.open[0] = self.lines.high[0] = self.lines.low[0] = self.lines.close[0] = 100
        self.lines.volume[0] = 100
        self.lines.openinterest[0] = 0
        return True


class OneOrderStrategy(bt.Strategy):
    def __init__(self):
        self.order = None
        self.statuses = []

    def next(self):
        if self.order is None:
            self.order = self.buy(size=2)

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())
        if order.status == order.Completed:
            self.env.runstop()


class ArmedOneOrderStrategy(bt.Strategy):
    params = (("intent_id", None), ("intent_resolver", None))

    def __init__(self):
        self.order = None
        self.statuses = []

    def next(self):
        if self.order is None:
            with self.p.intent_resolver.bind_next(self.p.intent_id):
                self.order = self.buy(size=2)

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())
        if order.status == order.Completed:
            self.env.runstop()


def _build(tmp_path, *, transport=None, cancel_transport=None, intent_resolver=None):
    path = tmp_path / "execution.sqlite"
    ledger = SQLiteOrderLedger(path)
    order = ledger.create_intent(OrderIntentInput(
        decision_id="run-1:event-1:0", broker="IBKR", account_ref="paper-fixture",
        environment=Environment.PAPER, deployment_id="deployment-1",
        strategy_version_hash=VERSION, input_event_seq=1, action_index=0,
        bar_end=NOW - timedelta(minutes=1), instrument_id="AAPL", side="BUY",
        integer_qty=2, reference_price=Decimal("100"), eligible_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5), risk_snapshot_id="b" * 64,
    ))
    ledger.set_execution_control(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        state=ExecutionControlState.RUNNING,
    )
    grant = RiskGrant.model_validate({
        "grant_id": "grant-1", "user_action_id": "user-action-1",
        "strategy_version_hash": VERSION, "broker": "IBKR", "account_ref": "paper-fixture",
        "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "1000", "max_gross_exposure": "5000",
        "strategy_budget": "2000", "max_daily_loss": "500", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(hours=1), "valid_until": NOW + timedelta(hours=2),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })
    facts = RiskFacts.model_validate({
        "broker": "IBKR", "account_ref": "paper-fixture", "environment": "paper",
        "strategy_version_hash": VERSION, "instrument_id": "AAPL", "now": NOW,
        "quote_as_of": NOW - timedelta(seconds=1), "account_as_of": NOW - timedelta(seconds=2),
        "quote_price": "100", "price_buffer_fraction": "0.01", "estimated_fees": "2",
        "net_liquidation": "10000", "available_cash": "5000", "positions": [],
        "strategy_position_qty": 0, "strategy_buy_reservations": "0",
        "account_buy_reservations": "0", "strategy_daily_loss": "0", "orders_this_session": 0,
        "account_snapshot_complete": True,
    })
    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda intent, **kwargs: DispatchRiskSnapshot(
            grant=grant, facts=facts, deployment_id="deployment-1"
        ),
        sender=lambda *args, **kwargs: pytest.fail("default sender must not be used"),
        now=lambda: NOW,
    )
    index = BrokerOrderIndex(path)
    events = Queue()
    delivered = []
    pump = IbkrCallbackPump(
        events=events, overflowed=Event(), ledger=ledger, order_index=index,
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        callback_sink=delivered.append, now=lambda: NOW,
    )
    sent = []
    owner_lease = ExecutionOwnerLease(
        lock_directory=tmp_path / "owner-locks", broker="IBKR",
        account_ref="paper-fixture", environment=Environment.PAPER,
        owner_id="test-execution-service",
    )
    observed_ids = []

    def fake_transport(command, **kwargs):
        # The binding must already be durable before this transport boundary.
        assert index.resolve(kwargs["broker_order_id"]).intent_id == order.intent_id
        sent.append((command, kwargs))
        events.put(_event(
            "execution", request_id=1, execution_id="exec-1", order_id=kwargs["broker_order_id"],
            account="paper-fixture", side="BOT", shares="2", price="101",
            time="20261020  11:00:00", symbol="AAPL", contract_id=123,
            security_type="STK", currency="USD",
        ))

    port = BacktraderDispatchPort(
        ledger=ledger, dispatch=dispatch, callback_pump=pump,
        resolve_intent=intent_resolver or (lambda command: order.intent_id),
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        owner_lease=owner_lease,
        allocate_order_id=lambda: 41,
        observe_next_valid_id=observed_ids.append,
        transport=transport or fake_transport,
        cancel_transport=cancel_transport,
        now=lambda: NOW,
    )
    port.register_callback_sink(delivered.append)
    command = BrokerOrderCommand(7, "AAPL", "BUY", 2)
    return ledger, order, index, port, command, sent, delivered, events, observed_ids


def test_native_order_is_checked_risk_dispatched_bound_before_send_and_callback_delivered(tmp_path):
    ledger, order, index, port, command, sent, delivered, events, observed_ids = _build(tmp_path)

    port.submit_order(command)

    assert ledger.get_order(order.intent_id).state is LocalOrderState.SEND_STARTED
    binding = index.resolve(41)
    assert (binding.intent_id, binding.bt_order_ref, binding.requested_qty, binding.side) == (
        order.intent_id, 7, 2, "BUY"
    )
    assert len(sent) == 1
    port.process_callbacks()
    assert ledger.get_order(order.intent_id).state is LocalOrderState.FILLED
    assert ledger.get_order(order.intent_id).filled_qty == 2
    assert delivered[-1].order_ref == 7
    assert delivered[-1].status == "FILLED"
    assert delivered[-1].average_fill_price == 101.0
    assert index.pending_inbox()  # queueing is not Backtrader consumption
    port.acknowledge_callback(delivered[-1].event_id)
    assert index.pending_inbox() == []
    events.put(_event("next_valid_id", order_id=42))
    port.process_callbacks()
    assert observed_ids == [42]
    port.owner_lease.close()


def test_non_usd_contract_fails_before_binding_order_id_or_transport(tmp_path):
    ledger, order, index, port, command, sent, _, _, _ = _build(tmp_path)
    port.resolve_contract_identity = lambda instrument: ContractIdentity(123, instrument, "STK", "EUR")
    allocated_ids = []
    port.allocate_order_id = lambda: allocated_ids.append(41) or 41

    with pytest.raises(PortOrderRejected, match="verified USD stock"):
        port.submit_order(command)

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CREATED
    assert allocated_ids == []
    with pytest.raises(CallbackPumpFatal, match="unmapped broker order"):
        index.resolve(41)
    with pytest.raises(CallbackPumpFatal, match="unmapped Backtrader order"):
        index.resolve_backtrader_ref(command.order_ref)
    assert sent == []
    port.owner_lease.close()


def test_dispatch_port_does_not_compete_with_dedicated_callback_worker(tmp_path):
    ledger, order, index, port, command, sent, delivered, events, _ = _build(tmp_path)
    events.put(_event("next_valid_id", order_id=51))
    worker = IbkrCallbackPumpWorker(port.callback_pump, poll_interval=0.001)

    port.process_callbacks()

    assert events.qsize() == 1
    assert delivered == []
    assert ledger.get_order(order.intent_id).state is LocalOrderState.CREATED
    assert port.callback_pump.background_worker_registered is True
    worker.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not port.callback_pump.next_valid_id_seen.is_set():
        time.sleep(0.001)
    assert port.callback_pump.next_valid_id_seen.is_set()
    assert worker.stop(timeout=1)
    port.owner_lease.close()


def test_native_order_mismatch_is_rejected_before_risk_or_transport(tmp_path):
    ledger, order, index, port, command, sent, delivered, _, _ = _build(tmp_path)

    with pytest.raises(PortOrderRejected, match="quantity"):
        port.submit_order(BrokerOrderCommand(7, "AAPL", "BUY", 3))

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CREATED
    assert sent == []
    with pytest.raises(Exception, match="unmapped broker order"):
        index.resolve(41)
    assert delivered == []
    port.owner_lease.close()


def test_native_order_cannot_dispatch_under_another_account_lease(tmp_path):
    ledger, order, index, port, command, sent, delivered, _, _ = _build(tmp_path)
    wrong_owner = ExecutionOwnerLease(
        lock_directory=tmp_path / "wrong-owner", broker="IBKR",
        account_ref="different-account", environment=Environment.PAPER,
        owner_id="wrong-owner",
    )
    port_with_wrong_owner = BacktraderDispatchPort(
        ledger=ledger, dispatch=port.dispatch, callback_pump=port.callback_pump,
        resolve_intent=lambda native: order.intent_id,
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        owner_lease=wrong_owner,
        allocate_order_id=lambda: 42,
        observe_next_valid_id=lambda _order_id: None,
        transport=lambda *args, **kwargs: pytest.fail("transport must not run"),
    )
    port_with_wrong_owner.register_callback_sink(delivered.append)

    with pytest.raises(PortOrderRejected, match="scope differs"):
        port_with_wrong_owner.submit_order(command)

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CREATED
    assert sent == []
    assert delivered == []
    wrong_owner.close()
    port.owner_lease.close()


def test_broker_id_collision_rolls_back_send_boundary_and_never_calls_transport(tmp_path):
    ledger, first_order, index, port, command, sent, delivered, _, _ = _build(tmp_path)
    port.submit_order(command)
    second_order = ledger.create_intent(ledger.get_intent(first_order.intent_id).model_copy(update={
        "decision_id": "run-1:event-2:0", "input_event_seq": 2,
        "bar_end": NOW, "risk_snapshot_id": "c" * 64,
    }))
    second_port = BacktraderDispatchPort(
        ledger=ledger, dispatch=port.dispatch, callback_pump=port.callback_pump,
        resolve_intent=lambda native: second_order.intent_id,
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        owner_lease=port.owner_lease,
        allocate_order_id=lambda: 41,  # deliberate collision with the first committed mapping
        observe_next_valid_id=lambda _order_id: None,
        transport=lambda *args, **kwargs: pytest.fail("transport must not run after mapping collision"),
    )
    second_port.register_callback_sink(delivered.append)

    with pytest.raises(PortOrderRejected, match="before the durable send boundary"):
        second_port.submit_order(BrokerOrderCommand(8, "AAPL", "BUY", 2))

    assert ledger.get_order(second_order.intent_id).state is LocalOrderState.CREATED
    assert ledger.get_order(first_order.intent_id).state is LocalOrderState.SEND_STARTED
    assert len(sent) == 1
    with pytest.raises(Exception, match="unmapped broker order"):
        index.resolve(42)
    port.owner_lease.close()


def test_cancel_is_persisted_once_for_owned_active_order_and_callback_confirms_it(tmp_path):
    cancel_calls = []
    ledger, order, index, port, command, _, delivered, events, _ = _build(
        tmp_path,
        transport=lambda *_args, **_kwargs: None,
        cancel_transport=cancel_calls.append,
    )
    port.submit_order(command)
    ledger.record_broker_event(BrokerEvent(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        intent_id=order.intent_id, broker_event_id="ack-before-cancel", kind=BrokerEventKind.ACK,
        occurred_at=NOW, received_at=NOW, broker_order_id="41",
    ))

    port.cancel_order(command)

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CANCEL_PENDING
    assert cancel_calls == [41]
    with pytest.raises(PortOrderRejected, match="cancel attempt already exists"):
        port.cancel_order(command)
    assert cancel_calls == [41]

    events.put(_event(
        "order_status", order_id=41, status="Cancelled", filled="0", remaining="2",
        average_fill_price="0",
    ))
    assert port.callback_pump.process_one().disposition == "APPLIED"
    assert ledger.get_order(order.intent_id).state is LocalOrderState.CANCELLED
    assert delivered[-1].status == "CANCELLED"
    port.owner_lease.close()


def test_uncertain_cancel_is_frozen_and_never_retried(tmp_path):
    cancel_calls = []

    def uncertain_cancel(order_id):
        cancel_calls.append(order_id)
        raise OSError("SDK outcome unknown")

    ledger, order, _, port, command, _, _, _, _ = _build(
        tmp_path,
        transport=lambda *_args, **_kwargs: None,
        cancel_transport=uncertain_cancel,
    )
    port.submit_order(command)
    ledger.record_broker_event(BrokerEvent(
        broker="IBKR", account_ref="paper-fixture", environment=Environment.PAPER,
        intent_id=order.intent_id, broker_event_id="ack-before-uncertain-cancel", kind=BrokerEventKind.ACK,
        occurred_at=NOW, received_at=NOW, broker_order_id="41",
    ))

    with pytest.raises(BrokerCommandUncertain, match="reconcile before retry"):
        port.cancel_order(command)
    with pytest.raises(PortOrderRejected, match="cancel attempt already exists"):
        port.cancel_order(command)

    assert cancel_calls == [41]
    assert ledger.get_order(order.intent_id).state is LocalOrderState.CANCEL_PENDING
    with ledger._connect() as connection:
        assert connection.execute(
            "SELECT state FROM cancel_attempt WHERE intent_id=?", (order.intent_id,)
        ).fetchone()[0] == "UNKNOWN"
    port.owner_lease.close()


def test_real_cerebro_native_order_reaches_ledger_and_returns_fill(tmp_path):
    ledger, order, index, port, _, sent, _, _, _ = _build(tmp_path)
    broker = PortBroker(port, initial_cash=10_000)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(TwoBarFeed(), name="AAPL")
    cerebro.setbroker(broker)
    cerebro.addstrategy(OneOrderStrategy)

    strategy = cerebro.run()[0]

    assert strategy.statuses[-2:] == ["Accepted", "Completed"]
    assert len(sent) == 1
    assert index.resolve(41).bt_order_ref == strategy.order.ref
    assert ledger.get_order(order.intent_id).state is LocalOrderState.FILLED
    assert broker.getcash() == pytest.approx(9_798.0)
    assert broker.getposition(strategy.data).size == 2
    port.owner_lease.close()


def test_one_shot_ledger_resolver_binds_real_cerebro_order_to_armed_intent(tmp_path):
    # Build once to obtain the exact ledger and risk fixture, then replace only
    # the trusted resolver before the real Cerebro order is created.
    ledger, order, index, port, _, sent, _, _, _ = _build(tmp_path)
    resolver = OneShotLedgerIntentResolver(ledger)
    port.resolve_intent = resolver
    broker = PortBroker(port, initial_cash=10_000)
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(TwoBarFeed(), name="AAPL")
    cerebro.setbroker(broker)
    cerebro.addstrategy(
        ArmedOneOrderStrategy,
        intent_id=order.intent_id,
        intent_resolver=resolver,
    )

    strategy = cerebro.run()[0]

    assert strategy.statuses[-2:] == ["Accepted", "Completed"]
    assert index.resolve(41).intent_id == order.intent_id
    assert index.resolve(41).bt_order_ref == strategy.order.ref
    assert len(sent) == 1
    port.owner_lease.close()


def test_one_shot_resolver_rejects_unarmed_reused_or_mismatched_native_orders(tmp_path):
    ledger, order, index, port, _, sent, _, _, _ = _build(tmp_path)
    resolver = OneShotLedgerIntentResolver(ledger)
    command = BrokerOrderCommand(8, "AAPL", "BUY", 2)

    with pytest.raises(PortOrderRejected, match="no host-armed"):
        resolver(command)
    with pytest.raises(PortOrderRejected, match="quantity"):
        with resolver.bind_next(order.intent_id):
            resolver(BrokerOrderCommand(8, "AAPL", "BUY", 3))
    with resolver.bind_next(order.intent_id):
        assert resolver(command) == order.intent_id
        with pytest.raises(PortOrderRejected, match="no host-armed"):
            resolver(command)

    assert ledger.get_order(order.intent_id).state is LocalOrderState.CREATED
    assert sent == []
    port.owner_lease.close()
