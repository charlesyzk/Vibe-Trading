from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from queue import Queue
from types import SimpleNamespace
from threading import Event

import backtrader as bt
import pytest

from src.bt_runtime.brokers.proxy import BrokerOrderCommand, PortBroker
from src.execution.backtrader_dispatch import BacktraderDispatchPort, OneShotLedgerIntentResolver
from src.execution.contracts import Bar, Environment, SourceKind, StrategySpec
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import PythonEventCoordinator
from src.execution.execution_service import PaperExecutionService
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.ibkr_callback_pump import BrokerOrderIndex, ContractIdentity, IbkrCallbackPump
from src.execution.order_ledger import ExecutionControlState, LocalOrderState, OrderIntentInput, SQLiteOrderLedger
from src.execution.outbox_dispatch import AuthorizedProposal, PythonOutboxIntentHandoff
from src.execution.paper_event_bridge import PaperEventBridgeError, PythonPaperEventBridge
from src.execution.risk import PositionFact, RiskDecision, RiskFacts
from src.execution.contracts import RiskGrant
from src.execution.versions import StrategyVersionStore


NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


class RecordingSandbox:
    def __init__(self, proposals):
        self.proposals = proposals
        self.requests = []

    def run_json(self, request):
        self.requests.append(request)
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"seen": 1},
            "order_proposals": self.proposals,
        })


class OneBarFeed(bt.feed.DataBase):
    def __init__(self):
        super().__init__()
        self.used = False

    def _load(self):
        if getattr(self, "index", 0) >= 2:
            return False
        self.index = getattr(self, "index", 0) + 1
        day = 20 + self.index
        self.lines.datetime[0] = bt.date2num(datetime(2026, 10, day, 15, 30))
        self.lines.open[0] = self.lines.high[0] = self.lines.low[0] = self.lines.close[0] = 100
        self.lines.volume[0] = 100
        self.lines.openinterest[0] = 0
        return True


class BridgeStrategy(bt.Strategy):
    params = (("bridge", None), ("bar", None), ("position_qty", 0))

    def start(self):
        if self.p.position_qty:
            self.broker.getposition(self.data).set(self.p.position_qty, 90.0)
            self.broker._cash -= self.p.position_qty * 90.0

    def next(self):
        if not hasattr(self, "bridge_result"):
            self.bridge_result = self.p.bridge.process_completed_bar(self, self.data, self.p.bar)

    def notify_order(self, order):
        if order.status == order.Completed:
            self.env.runstop()


def _bar(*, close="100"):
    return Bar(
        instrument_id="AAPL", session_date=date(2026, 10, 20),
        start_at=NOW.replace(hour=13, minute=30), end_at=NOW, available_at=NOW,
        timezone="America/New_York", open=Decimal(close), high=Decimal(close),
        low=Decimal(close), close=Decimal(close), volume=Decimal("1000"),
        currency="USD", is_final=True, source="fixture", adjustment="unadjusted",
    )


def _spec():
    return StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "bridge-test",
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


def _setup(tmp_path, proposals, *, strategy_position_qty=0):
    bar = _bar()
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(tmp_path / "journal.sqlite")
    journal.create_run(run_id="run-1", deployment_id="deployment-1", account_ref="paper-1",
                       environment=Environment.PAPER, strategy_version_hash=version.version_hash)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    service = PaperExecutionService(
        ledger=ledger, lock_directory=tmp_path / "locks", broker="IBKR", account_ref="paper-1",
        environment=Environment.PAPER, owner_id="bridge-test", recovery_check=lambda: True,
        activation_check=lambda: True, quiesce_check=lambda: True,
    )
    service.start()
    service.activate()
    sandbox = RecordingSandbox(proposals)
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    resolver = OneShotLedgerIntentResolver(ledger)

    def authorize(proposal):
        allowed = (
            proposal.proposal["side"] != "SELL"
            or proposal.proposal["integer_qty"] <= strategy_position_qty
        )
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker="IBKR", account_ref=proposal.identity["account_ref"],
            environment=Environment.PAPER, deployment_id=proposal.identity["deployment_id"],
            strategy_version_hash=proposal.identity["strategy_version_hash"],
            input_event_seq=proposal.seq, action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at, instrument_id=proposal.bar.instrument_id,
            side=proposal.proposal["side"], integer_qty=proposal.proposal["integer_qty"],
            order_type=proposal.proposal["order_type"], time_in_force=proposal.proposal["time_in_force"],
            reference_price=proposal.bar.close, eligible_at=NOW, expires_at=NOW.replace(day=21),
            risk_snapshot_id="a" * 64,
        )
        decision = RiskDecision(
            allowed=allowed,
            reason_codes=() if allowed else ("sell_exceeds_strategy_position",),
            checked_at=NOW, risk_snapshot_id="a" * 64,
            estimated_notional=Decimal("200"), estimated_cost=Decimal("200"),
            projected_strategy_exposure=Decimal("200"), projected_gross_exposure=Decimal("200"),
        )
        return AuthorizedProposal(intent=intent, decision=decision, broker="IBKR", reference_price=bar.close)

    handoff = PythonOutboxIntentHandoff(journal=journal, ledger=ledger, authorize=authorize)
    bridge = PythonPaperEventBridge(run_id="run-1", journal=journal, ledger=ledger,
                                    coordinator=coordinator, handoff=handoff,
                                    execution_service=service, intent_resolver=resolver)

    grant = RiskGrant.model_validate({
        "grant_id": "grant-bridge", "user_action_id": "approval-bridge",
        "strategy_version_hash": version.version_hash, "broker": "IBKR",
        "account_ref": "paper-1", "environment": "paper", "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "50000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW.replace(day=19), "valid_until": NOW.replace(day=21, hour=23),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })

    def facts_provider(intent, *, excluding_intent_id):
        assert excluding_intent_id
        facts = RiskFacts(
            broker="IBKR", account_ref="paper-1", environment=Environment.PAPER,
            strategy_version_hash=version.version_hash, instrument_id="AAPL", now=NOW,
            quote_as_of=NOW - timedelta(seconds=1), account_as_of=NOW - timedelta(seconds=2),
            quote_price=Decimal("100"), price_buffer_fraction=Decimal("0.01"),
            estimated_fees=Decimal("1"), net_liquidation=Decimal("100000"),
            available_cash=Decimal("50000"), positions=(PositionFact(
                instrument_id="AAPL", quantity=strategy_position_qty, market_price=Decimal("100"),
            ),), strategy_position_qty=strategy_position_qty, strategy_buy_reservations=Decimal("0"),
            account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
            orders_this_session=0, account_snapshot_complete=True,
        )
        return DispatchRiskSnapshot(grant=grant, facts=facts, deployment_id="deployment-1")

    dispatch = PaperOrderDispatchService(
        ledger=ledger, facts_provider=facts_provider,
        sender=lambda *args, **kwargs: pytest.fail("BacktraderDispatchPort must supply the sender"),
        now=lambda: NOW,
    )
    events = Queue()
    index = BrokerOrderIndex(ledger.path)
    delivered = []
    pump = IbkrCallbackPump(
        events=events, overflowed=Event(), ledger=ledger, order_index=index,
        broker="IBKR", account_ref="paper-1", environment=Environment.PAPER,
        callback_sink=delivered.append, now=lambda: NOW,
    )
    sent = []

    def fake_transport(command, **kwargs):
        assert index.resolve(kwargs["broker_order_id"]).intent_id == kwargs["intent_id"]
        sent.append((command, kwargs))
        execution_side = "BOT" if command.side == "BUY" else "SLD"
        execution_price = "101" if command.side == "BUY" else "99"
        events.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 1, "execution_id": "bridge-exec-1", "order_id": kwargs["broker_order_id"],
            "account": "paper-1", "side": execution_side, "shares": str(command.quantity), "price": execution_price,
            "time": "20261020  11:00:00", "symbol": "AAPL", "contract_id": 123,
            "security_type": "STK", "currency": "USD",
        }.items()))))

    port = BacktraderDispatchPort(
        ledger=ledger, dispatch=dispatch, callback_pump=pump, resolve_intent=resolver,
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        owner_lease=service.require_owner_lease(), allocate_order_id=lambda: 41,
        observe_next_valid_id=lambda _value: None, transport=fake_transport,
    )
    cerebro = bt.Cerebro(quicknotify=True, stdstats=False)
    cerebro.adddata(OneBarFeed(), name="AAPL")
    cerebro.setbroker(PortBroker(port, initial_cash=10_000))
    cerebro.addstrategy(BridgeStrategy, bridge=bridge, bar=bar, position_qty=strategy_position_qty)
    return cerebro, journal, ledger, service, sandbox, port, sent, index


def test_real_cerebro_event_bridge_hands_current_event_intent_to_native_order(tmp_path):
    cerebro, journal, ledger, service, sandbox, port, sent, index = _setup(
        tmp_path, [{"side": "BUY", "integer_qty": 2, "reason": "entry"}],
    )

    strategy = cerebro.run()[0]

    assert strategy.bridge_result.intent_id
    intent = ledger.get_intent(strategy.bridge_result.intent_id)
    assert intent.input_event_seq == 1
    assert intent.instrument_id == "AAPL"
    assert sent[0][0] == BrokerOrderCommand(strategy.bridge_result.native_order.ref, "AAPL", "BUY", 2)
    assert index.resolve(41).intent_id == strategy.bridge_result.intent_id
    assert strategy.bridge_result.native_order.status == strategy.bridge_result.native_order.Completed
    assert ledger.get_order(strategy.bridge_result.intent_id).state is LocalOrderState.FILLED
    assert strategy.bridge_result.native_order.executed.price == pytest.approx(101.0)
    assert sandbox.requests[0]["event"]["bar"]["close"] == "100"
    assert journal.get_run_identity("run-1")["account_ref"] == "paper-1"
    service.close()


def test_real_cerebro_event_bridge_routes_authorized_sell_to_native_order(tmp_path):
    cerebro, _journal, ledger, service, _sandbox, _port, sent, _index = _setup(
        tmp_path, [{"side": "SELL", "integer_qty": 2, "reason": "exit"}],
        strategy_position_qty=2,
    )

    strategy = cerebro.run()[0]

    intent_id = strategy.bridge_result.intent_id
    assert intent_id
    assert ledger.get_intent(intent_id).side == "SELL"
    assert sent[0][0] == BrokerOrderCommand(strategy.bridge_result.native_order.ref, "AAPL", "SELL", 2)
    assert strategy.bridge_result.native_order.status == strategy.bridge_result.native_order.Completed
    assert strategy.bridge_result.native_order.executed.size == -2
    assert strategy.bridge_result.native_order.executed.price == pytest.approx(99.0)
    assert ledger.get_order(intent_id).state is LocalOrderState.FILLED
    service.close()


def test_oversell_proposal_is_rejected_before_native_order_or_send(tmp_path):
    cerebro, _journal, ledger, service, _sandbox, _port, sent, _index = _setup(
        tmp_path, [{"side": "SELL", "integer_qty": 2, "reason": "oversell"}],
        strategy_position_qty=1,
    )

    with pytest.raises(PaperEventBridgeError, match="proposal did not resolve"):
        cerebro.run()

    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert sent == []
    assert service.state()[0] is ExecutionControlState.PAUSED
    service.close()


def test_multiple_proposals_pause_before_handoff_or_native_order(tmp_path):
    proposal = {"side": "BUY", "integer_qty": 2, "reason": "entry"}
    cerebro, journal, ledger, service, sandbox, port, sent, _index = _setup(tmp_path, [proposal, proposal])

    with pytest.raises(PaperEventBridgeError, match="at most one"):
        cerebro.run()

    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert sent == []
    assert sandbox.requests
    with pytest.raises(Exception, match="run is not running"):
        journal.get_run_identity("run-1")
    assert service.state()[0] is ExecutionControlState.PAUSED
    service.close()


def test_stale_recovered_event_pauses_before_worker_or_native_order(tmp_path):
    cerebro, journal, ledger, service, sandbox, port, sent, _index = _setup(
        tmp_path, [{"side": "BUY", "integer_qty": 2, "reason": "entry"}],
    )
    journal.stage_next_event("run-1", {"bar": _bar(close="99").model_dump(mode="json")},
                             event_id="recovered-old-event")

    with pytest.raises(PaperEventBridgeError, match="differs from the current completed bar"):
        cerebro.run()

    assert sandbox.requests == []
    assert sent == []
    assert ledger.recovery_snapshot().ready_to_reserve == ()
    assert service.state()[0] is ExecutionControlState.PAUSED
    service.close()
