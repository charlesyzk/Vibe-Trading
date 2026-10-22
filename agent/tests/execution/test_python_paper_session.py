from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from queue import Queue
from threading import Event
from types import SimpleNamespace

import backtrader as bt
import pytest

from src.bt_runtime.brokers import PortBroker
from src.bt_runtime.feeds.paper_queue import PaperFeedError
from src.execution.contracts import Environment, RiskGrant, SourceKind, StrategySpec, Bar
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.event_journal import SQLiteEventJournal
from src.execution.event_worker import PythonEventCoordinator
from src.execution.execution_service import PaperExecutionService
from src.execution.ibkr_callback_pump import BrokerOrderIndex, ContractIdentity, IbkrCallbackPump
from src.execution.order_ledger import LocalOrderState, OrderIntentInput, SQLiteOrderLedger
from src.execution.outbox_dispatch import AuthorizedProposal, PythonOutboxIntentHandoff
from src.execution.python_paper_session import PythonPaperSession, PythonPaperSessionError
from src.execution.risk import PositionFact, RiskDecision, RiskFacts
from src.execution.versions import StrategyVersionStore


NOW = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)


class NoProposalSandbox:
    def __init__(self, proposals=(), *, empty_events=0):
        self.requests = []
        self.proposals = list(proposals)
        self.empty_events = empty_events
        self.called = Event()

    def run_json(self, request):
        self.requests.append(request)
        self.called.set()
        if self.empty_events:
            self.empty_events -= 1
            proposals = []
        else:
            proposals, self.proposals = self.proposals, []
        return SimpleNamespace(response={
            "protocol_version": request["protocol_version"],
            "request_id": request["request_id"],
            "event_id": request["event_id"],
            "state": {"count": len(self.requests)},
            "order_proposals": proposals,
        })


class OfflineTransport:
    offline = True

    def __call__(self, *_args, **_kwargs):
        pytest.fail("no proposal should reach the transport")


def _spec():
    return StrategySpec.model_validate({
        "schema_version": "1.0",
        "strategy_id": "session-test",
        "source_kind": SourceKind.PYTHON_UPLOAD.value,
        "source_artifact_id": "upload-session-test",
        "instrument_ids": ["AAPL"],
        "bar_policy": {
            "timeframe": "1d", "session": "regular", "completed_only": True,
            "calendar_id": "us_equity_rth", "timezone": "America/New_York",
        },
        "parameters": {},
        "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
        "execution_policy": {
            "decision": "completed_bar_available", "dispatch": "next_regular_session",
            "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
            "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause",
        },
    })


def _bar(day=20, *, available_at=NOW):
    end_at = datetime(2026, 10, day, 20, 0, tzinfo=timezone.utc)
    return Bar(
        instrument_id="AAPL", session_date=date(2026, 10, day),
        start_at=end_at.replace(hour=13, minute=30), end_at=end_at, available_at=available_at,
        timezone="America/New_York", open=Decimal("100"), high=Decimal("102"),
        low=Decimal("99"), close=Decimal("101"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="verified-fixture", adjustment="unadjusted",
    )


def _build(tmp_path, *, journal_version=None, transport=None, proposals=(), proposal_delay=0, queue_size=16):
    clock = [NOW]
    versions = StrategyVersionStore(tmp_path / "versions")
    version = versions.create_version(
        _spec(), upload_source=b"def on_event(event, state): return {'state': state}\n",
        engine_version="backtrader/1.9.78.123", compiler_version="vibe-python-event/1",
    )
    journal = SQLiteEventJournal(tmp_path / "journal.sqlite")
    journal.create_run(
        run_id="session-run", deployment_id="session-deployment", account_ref="paper-session",
        environment=Environment.PAPER,
        strategy_version_hash=journal_version or version.version_hash,
    )
    ledger = SQLiteOrderLedger(tmp_path / "orders.sqlite")
    service = PaperExecutionService(
        ledger=ledger, lock_directory=tmp_path / "owner-locks", broker="IBKR",
        account_ref="paper-session", environment=Environment.PAPER,
        owner_id="offline-session-test", recovery_check=lambda: True,
        activation_check=lambda: True, quiesce_check=lambda: True,
    )
    service.start()
    service.activate()
    sandbox = NoProposalSandbox(proposals, empty_events=proposal_delay)
    coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
    def authorize(proposal):
        intent = OrderIntentInput(
            decision_id=f"{proposal.run_id}:{proposal.event_id}:{proposal.proposal_index}",
            broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
            deployment_id="session-deployment", strategy_version_hash=version.version_hash,
            input_event_seq=proposal.seq, action_index=proposal.proposal_index,
            bar_end=proposal.bar.end_at, instrument_id=proposal.bar.instrument_id,
            side=proposal.proposal["side"], integer_qty=proposal.proposal["integer_qty"],
            order_type=proposal.proposal["order_type"], time_in_force=proposal.proposal["time_in_force"],
            reference_price=proposal.bar.close, eligible_at=NOW, expires_at=NOW + timedelta(days=3),
            risk_snapshot_id="a" * 64,
        )
        decision = RiskDecision(
            allowed=True, reason_codes=(), checked_at=clock[0], risk_snapshot_id="a" * 64,
            estimated_notional=Decimal("202"), estimated_cost=Decimal("203"),
            projected_strategy_exposure=Decimal("203"), projected_gross_exposure=Decimal("203"),
        )
        return AuthorizedProposal(intent, decision, "IBKR", proposal.bar.close)

    handoff = PythonOutboxIntentHandoff(
        journal=journal, ledger=ledger, authorize=authorize,
    )
    index = BrokerOrderIndex(ledger.path)
    event_queue = Queue()
    pump = IbkrCallbackPump(
        events=event_queue, overflowed=Event(), ledger=ledger, order_index=index,
        broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
        callback_sink=lambda _callback: None, now=lambda: clock[0],
    )

    sent = []
    def send(command, **kwargs):
        sent.append((command, kwargs))
        event_queue.put(SimpleNamespace(kind="execution", payload=tuple(sorted({
            "request_id": 1, "execution_id": "session-exec-1", "order_id": kwargs["broker_order_id"],
            "account": "paper-session", "side": "BOT", "shares": str(command.quantity),
            "price": "101", "time": "20261020  11:00:00", "symbol": "AAPL",
            "contract_id": 123, "security_type": "STK", "currency": "USD",
        }.items()))))

    dispatch = PaperOrderDispatchService(
        ledger=ledger,
        facts_provider=lambda intent, **_kwargs: DispatchRiskSnapshot(
            grant=RiskGrant.model_validate({
                "grant_id": "session-grant", "user_action_id": "session-approval",
                "strategy_version_hash": version.version_hash, "broker": "IBKR",
                "account_ref": "paper-session", "environment": "paper",
                "allowed_instruments": ["AAPL"], "allowed_order_types": ["market"],
                "long_only": True, "whole_shares": True, "max_order_notional": "10000",
                "max_gross_exposure": "100000", "strategy_budget": "50000",
                "max_daily_loss": "1000", "max_orders_per_session": 10,
                "valid_from": NOW - timedelta(days=1), "valid_until": NOW + timedelta(days=3),
                "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
                "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
            }),
            facts=RiskFacts(
                broker="IBKR", account_ref="paper-session", environment=Environment.PAPER,
                strategy_version_hash=version.version_hash, instrument_id=intent.instrument_id,
                now=clock[0], quote_as_of=clock[0] - timedelta(seconds=1),
                account_as_of=clock[0] - timedelta(seconds=2), quote_price=Decimal("101"),
                price_buffer_fraction=Decimal("0.01"), estimated_fees=Decimal("1"),
                net_liquidation=Decimal("100000"), available_cash=Decimal("50000"),
                positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=Decimal("101")),),
                strategy_position_qty=0, strategy_buy_reservations=Decimal("0"),
                account_buy_reservations=Decimal("0"), strategy_daily_loss=Decimal("0"),
                orders_this_session=0, account_snapshot_complete=True,
            ),
            deployment_id="session-deployment",
        ),
        sender=lambda *_args, **_kwargs: pytest.fail("dispatch port must supply the sender"),
        now=lambda: clock[0],
    )
    resolved_transport = transport or OfflineTransport()
    if proposals:
        resolved_transport = send
        resolved_transport.offline = True
    try:
        session = PythonPaperSession(
            run_id="session-run", version_store=versions, version_id=version.version_id,
            journal=journal, ledger=ledger, coordinator=coordinator, handoff=handoff,
            execution_service=service, dispatch=dispatch, callback_pump=pump,
            resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
            allocate_order_id=lambda: 41,
            observe_next_valid_id=lambda _value: None,
            transport=resolved_transport, now=lambda: clock[0],
            queue_size=queue_size,
        )
    except Exception:
        service.close()
        raise
    return session, version, service, sandbox, ledger, sent, clock


def test_explicit_session_pins_version_and_runs_host_pushed_bar_in_real_cerebro(tmp_path):
    session, version, service, sandbox, _ledger, _sent, _clock = _build(tmp_path)
    session.push_completed_bar(_bar())
    strategy = session.run()

    assert isinstance(session.cerebro, bt.Cerebro)
    assert isinstance(session.broker, PortBroker)
    assert session.version_hash == version.version_hash
    assert strategy.results[0].outcome.event.seq == 1
    assert strategy.results[0].intent_id is None
    assert sandbox.requests[0]["event"]["bar"]["instrument_id"] == "AAPL"
    assert sandbox.requests[0]["source"] == "def on_event(event, state): return {'state': state}\n"
    assert session.feed.current_completed_bar == _bar()
    service.close()


def test_session_rejects_journal_bound_to_another_frozen_version(tmp_path):
    with pytest.raises(PythonPaperSessionError, match="journal run identity"):
        _build(tmp_path, journal_version="f" * 64)


def test_session_rejects_transport_without_explicit_offline_marker(tmp_path):
    with pytest.raises(PythonPaperSessionError, match="marked offline=True"):
        _build(tmp_path, transport=lambda *_args, **_kwargs: None)


def test_worker_proposal_creates_native_portbroker_order_with_frozen_version(tmp_path):
    session, version, service, sandbox, ledger, sent, clock = _build(
        tmp_path, proposals=[{"side": "BUY", "integer_qty": 2, "reason": "entry"}],
        proposal_delay=1,
    )
    session.push_completed_bar(_bar())
    clock[0] = NOW + timedelta(days=1)
    session.push_completed_bar(_bar(21, available_at=clock[0]))
    clock[0] = NOW + timedelta(days=2)
    session.push_completed_bar(_bar(22, available_at=clock[0]))
    strategy = session.run()

    result = next(item for item in strategy.results if item.intent_id is not None)
    assert result.intent_id is not None
    assert result.native_order is not None
    assert result.native_order.status == result.native_order.Completed
    assert ledger.get_intent(result.intent_id).strategy_version_hash == version.version_hash
    assert ledger.get_order(result.intent_id).state is LocalOrderState.FILLED
    assert sent[0][0].side == "BUY" and sent[0][0].quantity == 2
    assert sandbox.requests[0]["source"].startswith("def on_event")
    assert session.port.transport.offline is True
    service.close()


def test_session_preserves_feed_order_and_finish_input_boundary(tmp_path):
    session, _version, service, _sandbox, _ledger, _sent, _clock = _build(tmp_path)
    session.push_completed_bar(_bar())
    with pytest.raises(PaperFeedError, match="strictly increasing"):
        session.push_completed_bar(_bar(19))
    assert service.state()[0].value == "PAUSED"
    service.close()


def test_session_rejects_bar_after_explicit_input_finish(tmp_path):
    session, _version, service, _sandbox, _ledger, _sent, _clock = _build(tmp_path)
    session.finish_input()
    with pytest.raises(PythonPaperSessionError, match="input is finished"):
        session.push_completed_bar(_bar())
    service.close()


def test_session_starts_real_cerebro_thread_accepts_host_bars_then_stops_and_joins(tmp_path):
    session, _version, service, sandbox, _ledger, _sent, clock = _build(tmp_path)
    session.start()
    assert session.is_alive
    with pytest.raises(PythonPaperSessionError, match="start once"):
        session.start()

    session.push_completed_bar(_bar())
    assert sandbox.called.wait(2), "strategy worker did not receive the host-pushed bar"
    assert session.is_alive

    clock[0] += timedelta(days=1)
    second = _bar(21, available_at=clock[0])
    session.push_completed_bar(second)
    session.stop()
    assert session.join(3)
    assert not session.is_alive
    assert session.exception is None
    assert session.strategy is not None
    assert len(session.strategy.results) == 2
    assert [item.outcome.event.seq for item in session.strategy.results] == [1, 2]
    with pytest.raises(PythonPaperSessionError, match="input is finished"):
        session.push_completed_bar(_bar(22, available_at=clock[0] + timedelta(days=1)))
    service.close()


def test_session_background_feed_overflow_fails_closed_and_is_observable(tmp_path):
    session, _version, service, _sandbox, _ledger, _sent, clock = _build(tmp_path, queue_size=1)
    session.push_completed_bar(_bar())
    clock[0] += timedelta(days=1)
    with pytest.raises(PaperFeedError, match="queue is full"):
        session.push_completed_bar(_bar(21, available_at=clock[0]))
    session.start()
    assert session.join(3)
    assert isinstance(session.exception, PaperFeedError)
    assert "queue is full" in str(session.exception)
    assert session.strategy is None
    assert service.state()[0].value == "PAUSED"
    service.close()
