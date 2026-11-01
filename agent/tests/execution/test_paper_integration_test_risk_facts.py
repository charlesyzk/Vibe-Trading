from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.contracts import Environment
from src.execution.order_ledger import OrderIntentInput, SQLiteOrderLedger
from src.execution.paper_integration_test_config import PaperIntegrationTestConfig
from src.execution.paper_integration_test_risk import (
    PaperIntegrationTestRiskFactsError,
    PaperIntegrationTestRiskFactsProvider,
)
from tests.execution.test_paper_integration_test_config import _payload


NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
VERSION = "a" * 64
DEPLOYMENT = "paper-aapl-integration-test"


def make_snapshot(**changes):
    value = {
        "capture_complete": True,
        "reconciled": True,
        "account_ref": "DUR123456",
        "environment": "paper",
        "facts_sha256": "b" * 64,
        "completed_at_utc": NOW.isoformat(),
        "account_summary": [
            {"tag": "NetLiquidation", "currency": "HKD", "value": "100000"},
            {"tag": "TotalCashValue", "currency": "HKD", "value": "90000"},
            {"tag": "AvailableFunds", "currency": "HKD", "value": "80000"},
        ],
        "positions": [],
        "open_orders": [],
    }
    value.update(changes)
    return value


def make_intent(**changes):
    value = {
        "broker": "IBKR",
        "account_ref": "DUR123456",
        "environment": Environment.PAPER,
        "deployment_id": DEPLOYMENT,
        "strategy_version_hash": VERSION,
        "instrument_id": "AAPL",
        "side": "BUY",
        "integer_qty": 1,
        "order_type": "limit",
        "time_in_force": "DAY",
        "limit_price": Decimal("400.00"),
        "decision_id": "paper-aapl-integration-test:1:0",
        "input_event_seq": 1,
        "action_index": 0,
        "bar_end": NOW - timedelta(days=1),
        "reference_price": Decimal("400.00"),
        "eligible_at": NOW,
        "expires_at": NOW.replace(hour=21),
        "risk_snapshot_id": "c" * 64,
    }
    value.update(changes)
    return OrderIntentInput.model_validate(value)


def make_provider(tmp_path, *, snapshot=None, enabled=True, now=NOW):
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = _payload()
    payload["enabled"] = enabled
    config = PaperIntegrationTestConfig.model_validate(payload)
    ledger = SQLiteOrderLedger(tmp_path / "ledger.sqlite")
    return PaperIntegrationTestRiskFactsProvider(
        config=config,
        strategy_version_hash=VERSION,
        deployment_id=DEPLOYMENT,
        snapshot_reader=lambda: snapshot or make_snapshot(),
        ledger=ledger,
        now=lambda: now,
    )


def dispatch(provider, *, intent=None):
    selected = intent or make_intent()
    record = provider.ledger.create_intent(selected)
    return provider(selected, excluding_intent_id=record.intent_id)


def test_builds_exact_limit_only_risk_grant_from_fresh_hkd_reconciled_snapshot(tmp_path):
    result = dispatch(make_provider(tmp_path))

    assert result.grant.funds_check == "broker_paper_limit"
    assert result.grant.allowed_instruments == ("AAPL",)
    assert result.grant.allowed_order_types == ("limit",)
    assert result.grant.max_order_notional == Decimal("400")
    assert result.facts.quote_price is None
    assert result.facts.quote_as_of is None
    assert result.facts.available_cash is None
    assert result.facts.net_liquidation is None
    assert result.facts.account_snapshot_complete is True
    assert result.deployment_id == DEPLOYMENT


@pytest.mark.parametrize(
    ("snapshot_change", "message"),
    [
        ({"reconciled": False}, "complete, reconciled"),
        ({"account_ref": "DU999"}, "complete, reconciled"),
        ({"facts_sha256": "bad"}, "complete, reconciled"),
        ({"positions": [{"contract_id": 265598}]}, "zero account positions"),
        ({"open_orders": [{"order_id": 12}]}, "zero account positions"),
        ({"account_summary": []}, "required account summary tags"),
        ({"completed_at_utc": (NOW - timedelta(seconds=11)).isoformat()}, "stale or from the future"),
    ],
)
def test_rejects_incomplete_or_conflicting_account_facts(tmp_path, snapshot_change, message):
    provider = make_provider(tmp_path, snapshot=make_snapshot(**snapshot_change))
    with pytest.raises(PaperIntegrationTestRiskFactsError, match=message):
        dispatch(provider)


def test_rejects_disabled_or_mismatched_one_shot_intent(tmp_path):
    provider = make_provider(tmp_path, enabled=False)
    with pytest.raises(PaperIntegrationTestRiskFactsError, match="disabled or outside"):
        dispatch(provider)

    provider = make_provider(tmp_path)
    with pytest.raises(PaperIntegrationTestRiskFactsError, match="exact one-shot"):
        dispatch(make_provider(tmp_path / "mismatch"), intent=make_intent(integer_qty=2))


def test_rejects_account_summary_with_mixed_currencies(tmp_path):
    snapshot = make_snapshot()
    snapshot["account_summary"][1]["currency"] = "USD"
    provider = make_provider(tmp_path, snapshot=snapshot)

    with pytest.raises(PaperIntegrationTestRiskFactsError, match="currency-mixed"):
        dispatch(provider)


def test_ledger_history_lookup_is_scope_bound_and_can_exclude_current_intent(tmp_path):
    provider = make_provider(tmp_path)
    record = provider.ledger.create_intent(make_intent())

    assert provider.ledger.has_strategy_version_intent(
        broker="IBKR",
        account_ref="DUR123456",
        environment=Environment.PAPER,
        strategy_version_hash=VERSION,
    )
    assert not provider.ledger.has_strategy_version_intent(
        broker="IBKR",
        account_ref="DU999",
        environment=Environment.PAPER,
        strategy_version_hash=VERSION,
    )
    assert not provider.ledger.has_strategy_version_intent(
        broker="IBKR",
        account_ref="DUR123456",
        environment=Environment.PAPER,
        strategy_version_hash=VERSION,
        excluding_intent_id=record.intent_id,
    )
