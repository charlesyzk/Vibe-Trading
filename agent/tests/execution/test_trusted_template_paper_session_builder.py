from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.bt_runtime.strategies.sma import (
    TRUSTED_TEMPLATE_COMPILER_VERSION,
    TRUSTED_TEMPLATE_SOURCE,
)
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Bar, Environment, RiskGrant, StrategySpec
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.event_journal import SQLiteEventJournal
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.outbox_dispatch import ProposalRiskInputs
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.risk import PositionFact, RiskFacts
from src.execution.trusted_template_paper import (
    TargetFractionPaperSizer,
    TrustedTemplateEventCoordinator,
)
from src.execution.trusted_template_paper_session_builder import (
    TrustedTemplatePaperSessionBuildError,
    TrustedTemplatePaperSessionBuilder,
)
from src.execution.versions import StrategyVersionStore
from tests.execution.test_managed_python_paper_session import _make_runtime, _spec


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
ENGINE_VERSION = "backtrader/1.9.78.123"


def _nl2_spec(*, source_kind="natural_language_template", target_fraction=True):
    if source_kind == "python_upload":
        return _spec()
    position = (
        {
            "mode": "target_fraction", "long_only": True, "whole_shares": True,
            "allocation_fraction": "0.2", "rebalance": "on_signal_transition",
            "initial_entry": "wait_for_cross",
        }
        if target_fraction
        else {"mode": "explicit_quantity", "long_only": True, "whole_shares": True}
    )
    return StrategySpec.model_validate({
        "schema_version": "1.0", "strategy_id": "builder-nl2-sma",
        "source_kind": "natural_language_template", "template_id": "sma_cross", "template_version": "1.0",
        "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
        "parameters": {"fast_period": 1, "slow_period": 2},
        "position_policy": position,
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "skip", "missed_session_action": "expire"},
    })


def _version_store(tmp_path, *, source_kind="natural_language_template", target_fraction=True, artifact=None):
    store = StrategyVersionStore(tmp_path / "versions")
    spec = _nl2_spec(source_kind=source_kind, target_fraction=target_fraction)
    if source_kind == "python_upload":
        record = store.create_version(
            spec, upload_source=b"def on_event(event, state): return {}\n",
            engine_version=ENGINE_VERSION, compiler_version="vibe-python-event/1",
        )
    else:
        record = store.create_version(
            spec, template_source=TRUSTED_TEMPLATE_SOURCE if artifact is None else artifact,
            engine_version=ENGINE_VERSION,
            compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
        )
    return store, record


def _providers(version_hash, *, account_ref="paper-session", environment=Environment.PAPER):
    grant = RiskGrant.model_validate({
        "grant_id": "builder-grant", "user_action_id": "builder-user-action",
        "strategy_version_hash": version_hash, "broker": "IBKR", "account_ref": account_ref,
        "environment": environment.value, "allowed_instruments": ["AAPL"],
        "allowed_order_types": ["market"], "long_only": True, "whole_shares": True,
        "max_order_notional": "10000", "max_gross_exposure": "100000",
        "strategy_budget": "5000", "max_daily_loss": "1000", "max_orders_per_session": 10,
        "valid_from": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=1),
        "quote_max_age_seconds": 30, "account_max_age_seconds": 60,
        "halt_action": "pause_and_cancel_owned", "liquidation_policy": "none", "granted_at": NOW,
    })
    calls = []

    def risk_inputs(proposal):
        calls.append(proposal)
        facts = RiskFacts(
            broker="IBKR", account_ref=account_ref, environment=environment,
            strategy_version_hash=version_hash, instrument_id=proposal.bar.instrument_id,
            now=NOW, quote_as_of=NOW, account_as_of=NOW,
            quote_price=Decimal(str(proposal.bar.close)), price_buffer_fraction=Decimal("0.01"),
            estimated_fees=Decimal("1"), net_liquidation=Decimal("10000"),
            available_cash=Decimal("9000"),
            positions=(PositionFact(instrument_id="AAPL", quantity=0, market_price=proposal.bar.close),),
            strategy_position_qty=0, strategy_sell_reservations=0,
            strategy_buy_reservations=Decimal("0"), account_buy_reservations=Decimal("0"),
            strategy_daily_loss=Decimal("0"), orders_this_session=0, account_snapshot_complete=True,
        )
        return ProposalRiskInputs(
            grant=grant, facts=facts, eligible_at=NOW,
            expires_at=NOW + timedelta(minutes=30),
        )

    def dispatch_facts(intent, *, excluding_intent_id):
        calls.append((intent, excluding_intent_id))
        inputs = risk_inputs(type("Proposal", (), {
            "bar": type("Bar", (), {"instrument_id": intent.instrument_id, "close": intent.reference_price})(),
        })())
        return DispatchRiskSnapshot(
            grant=grant, facts=inputs.facts, deployment_id="builder-deployment",
        )

    return risk_inputs, dispatch_facts, calls


def _builder(tmp_path, store, record, *, risk_inputs=None, dispatch_facts=None, account_ref="paper-session"):
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", queue_size=4,
    )
    risk_inputs = risk_inputs or _providers(record.version_hash, account_ref=account_ref)[0]
    dispatch_facts = dispatch_facts or _providers(record.version_hash, account_ref=account_ref)[1]
    return TrustedTemplatePaperSessionBuilder(
        version_store=store,
        version_id=record.version_id,
        run_id="builder-run",
        deployment_id="builder-deployment",
        proposal_risk_inputs=risk_inputs,
        dispatch_risk_facts=dispatch_facts,
        feeds={"AAPL": feed},
        data_by_con_id={123: feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        now=lambda: NOW,
    )


def test_builder_composes_trusted_nl2_session_on_runtime_database_without_starting(tmp_path):
    store, record = _version_store(tmp_path)
    risk_inputs, dispatch_facts, calls = _providers(record.version_hash)
    builder = _builder(
        tmp_path, store, record,
        risk_inputs=risk_inputs, dispatch_facts=dispatch_facts,
    )
    runtime, service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-runtime.sqlite",
    )

    session = builder(runtime, PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER))

    assert isinstance(session, ManagedPythonPaperSession)
    assert isinstance(session.coordinator, TrustedTemplateEventCoordinator)
    assert isinstance(session.coordinator.size_signal, TargetFractionPaperSizer)
    assert session.handoff.journal is session.journal
    assert session.handoff.ledger is runtime.ledger
    assert session.dispatch_service.ledger is runtime.ledger
    assert isinstance(session.dispatch_service, PaperOrderDispatchService)
    assert session.journal.path == runtime.ledger.path
    assert session.journal.get_run_identity("builder-run")["deployment_id"] == "builder-deployment"
    assert session.record.version_id == record.version_id
    assert calls == []
    assert runtime.process.pid is None
    assert runtime._started is False
    assert service.lease_held is False
    allocator.close()


def test_builder_requires_all_explicit_providers_feeds_and_contract_resolver(tmp_path):
    store, record = _version_store(tmp_path)
    risk_inputs, dispatch_facts, _calls = _providers(record.version_hash)
    with pytest.raises(TypeError, match="dispatch_risk_facts"):
        TrustedTemplatePaperSessionBuilder(
            version_store=store, version_id=record.version_id, run_id="run",
            deployment_id="deployment", proposal_risk_inputs=risk_inputs,
            feeds={"AAPL": object()}, data_by_con_id={123: object()},
            resolve_contract_identity=lambda _instrument: None, now=lambda: NOW,
        )
    with pytest.raises(TypeError, match="completed-bar feeds"):
        TrustedTemplatePaperSessionBuilder(
            version_store=store, version_id=record.version_id, run_id="run",
            deployment_id="deployment", proposal_risk_inputs=risk_inputs,
            dispatch_risk_facts=dispatch_facts, feeds={}, data_by_con_id={123: object()},
            resolve_contract_identity=lambda _instrument: None, now=lambda: NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("queue_size", 0, "queue_size"),
        ("queue_size", True, "queue_size"),
        ("idle_poll_seconds", 0.009, "idle_poll_seconds"),
        ("idle_poll_seconds", 5.001, "idle_poll_seconds"),
        ("idle_poll_seconds", True, "idle_poll_seconds"),
    ],
)
def test_builder_rejects_invalid_session_queue_settings_before_composition(
    tmp_path, field, value, message,
):
    store, record = _version_store(tmp_path)
    risk_inputs, dispatch_facts, _calls = _providers(record.version_hash)
    feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York")
    kwargs = {
        "version_store": store,
        "version_id": record.version_id,
        "run_id": "builder-invalid-queue-run",
        "deployment_id": "builder-deployment",
        "proposal_risk_inputs": risk_inputs,
        "dispatch_risk_facts": dispatch_facts,
        "feeds": {"AAPL": feed},
        "data_by_con_id": {123: feed},
        "resolve_contract_identity": lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        "now": lambda: NOW,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        TrustedTemplatePaperSessionBuilder(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_kind": "python_upload"}, "frozen NL2 target_fraction template"),
        ({"target_fraction": False}, "frozen NL2 target_fraction template"),
        ({"artifact": b"tampered trusted template"}, "frozen NL2 target_fraction template"),
    ],
)
def test_builder_rejects_non_nl2_or_tampered_frozen_versions(tmp_path, kwargs, message):
    store, record = _version_store(tmp_path, **kwargs)
    builder = _builder(tmp_path, store, record)
    runtime, _service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-reject.sqlite",
    )
    with pytest.raises(TrustedTemplatePaperSessionBuildError, match=message):
        builder(runtime, PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER))
    assert runtime.process.pid is None
    allocator.close()


def test_builder_rejects_unsupported_bar_policy_before_persisting_run(tmp_path):
    spec = _nl2_spec()
    unsupported = spec.model_copy(update={
        "bar_policy": spec.bar_policy.model_copy(update={"calendar_id": "other_calendar"}),
    })
    store = StrategyVersionStore(tmp_path / "versions")
    record = store.create_version(
        unsupported, template_source=TRUSTED_TEMPLATE_SOURCE,
        engine_version=ENGINE_VERSION, compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    builder = _builder(tmp_path, store, record)
    runtime, _service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-policy.sqlite",
    )

    with pytest.raises(TrustedTemplatePaperSessionBuildError, match="frozen NL2 target_fraction template"):
        builder(runtime, PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER))

    with sqlite3.connect(runtime.ledger.path) as connection:
        journal_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_run'"
        ).fetchall()
    assert journal_tables == []
    assert runtime.process.pid is None
    allocator.close()


def test_builder_rejects_runtime_scope_and_database_mismatch(tmp_path):
    store, record = _version_store(tmp_path)
    builder = _builder(tmp_path, store, record)
    runtime, _service, _ledger, index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-mismatch.sqlite",
    )
    with pytest.raises(TrustedTemplatePaperSessionBuildError, match="requested scope"):
        builder(runtime, PaperRuntimeScope("IBKR", "other-account", Environment.PAPER))

    original_path = index.path
    index.path = str(tmp_path / "other.sqlite")
    with pytest.raises(TrustedTemplatePaperSessionBuildError, match="one SQLite database"):
        builder(runtime, PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER))
    index.path = original_path
    assert runtime.process.pid is None
    allocator.close()


def test_builder_rejects_non_paper_scope_before_creating_journal_or_run(tmp_path):
    store, record = _version_store(tmp_path)
    builder = _builder(tmp_path, store, record)
    runtime, _service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-live-scope.sqlite",
    )
    invalid_scope = object.__new__(PaperRuntimeScope)
    object.__setattr__(invalid_scope, "broker", "IBKR")
    object.__setattr__(invalid_scope, "account_ref", "paper-session")
    object.__setattr__(invalid_scope, "environment", Environment.LIVE)

    with pytest.raises(TrustedTemplatePaperSessionBuildError, match="IBKR/PAPER scope"):
        builder(runtime, invalid_scope)

    with sqlite3.connect(runtime.ledger.path) as connection:
        journal_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_run'"
        ).fetchall()
    assert journal_tables == []
    assert runtime.process.pid is None
    allocator.close()


@pytest.mark.parametrize(
    ("provider_account", "provider_version", "expected_error"),
    [
        ("another-account", None, "proposal grant/account/quote facts"),
        (None, "b" * 64, "proposal grant/account/quote facts"),
    ],
)
def test_builder_rejects_proposal_provider_identity_drift_at_event_sizing(
    tmp_path, provider_account, provider_version, expected_error,
):
    store, record = _version_store(tmp_path)
    facts_hash = provider_version or record.version_hash
    risk_inputs, _bad_dispatch, _calls = _providers(
        facts_hash, account_ref=provider_account or "paper-session",
    )
    good_dispatch = _providers(record.version_hash)[1]
    builder = _builder(
        tmp_path, store, record,
        risk_inputs=risk_inputs, dispatch_facts=good_dispatch,
    )
    runtime, _service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "provider-identity.sqlite",
    )
    scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
    session = builder(runtime, scope)
    bar = Bar(
        instrument_id="AAPL", session_date="2026-10-20",
        start_at=datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        available_at=datetime(2026, 10, 20, 20, 1, tzinfo=timezone.utc),
        timezone="America/New_York", open=Decimal("100"), high=Decimal("101"),
        low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"),
        currency="USD", is_final=True, source="injected-test-feed", adjustment="raw",
    )
    with pytest.raises(TrustedTemplatePaperSessionBuildError, match=expected_error):
        session.coordinator.size_signal(
            side="BUY", spec=record.spec, run_id="builder-run", event_id="event-1",
            seq=1, identity=session.journal.get_run_identity("builder-run"), bar=bar,
        )
    assert runtime.process.pid is None
    allocator.close()


def test_builder_rejects_existing_run_with_different_version_or_deployment(tmp_path):
    store, record = _version_store(tmp_path)
    runtime, _service, _ledger, _index, allocator = _make_runtime(
        tmp_path, tmp_path / "builder-run-identity.sqlite",
    )
    scope = PaperRuntimeScope("IBKR", "paper-session", Environment.PAPER)
    _builder(tmp_path, store, record)(runtime, scope)

    other_spec = _nl2_spec()
    other_spec = other_spec.model_copy(update={"strategy_id": "another-nl2-strategy"})
    other_record = store.create_version(
        other_spec, template_source=TRUSTED_TEMPLATE_SOURCE,
        engine_version=ENGINE_VERSION, compiler_version=TRUSTED_TEMPLATE_COMPILER_VERSION,
    )
    other_feed = PaperCompletedBarFeed(instrument_id="AAPL", timezone="America/New_York")
    other_builder = TrustedTemplatePaperSessionBuilder(
        version_store=store, version_id=other_record.version_id,
        run_id="builder-run", deployment_id="another-deployment",
        proposal_risk_inputs=_providers(other_record.version_hash)[0],
        dispatch_risk_facts=_providers(other_record.version_hash)[1],
        feeds={"AAPL": other_feed},
        data_by_con_id={123: other_feed},
        resolve_contract_identity=lambda instrument: ContractIdentity(123, instrument, "STK", "USD"),
        now=lambda: NOW,
    )
    with pytest.raises(TrustedTemplatePaperSessionBuildError, match="differs from the pinned"):
        other_builder(runtime, scope)
    allocator.close()
