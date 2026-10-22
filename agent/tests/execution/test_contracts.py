from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.execution.contracts import (
    Environment,
    EventEnvelope,
    LocalSubmissionState,
    OrderIntent,
    PositionPolicy,
    RiskGrant,
    SourceKind,
    StrategySpec,
    RunState,
    BrokerOrderState,
    Bar,
    DataManifest,
    canonical_json,
    canonical_sha256,
)


def make_strategy(**overrides):
    value = {
        "schema_version": "1.0",
        "strategy_id": "sma_demo",
        "source_kind": "natural_language_template",
        "template_id": "sma_cross",
        "template_version": "1.0",
        "instrument_ids": ["us_equity_demo"],
        "bar_policy": {
            "timeframe": "1d",
            "session": "regular",
            "completed_only": True,
            "calendar_id": "us_equity_rth",
            "timezone": "America/New_York",
        },
        "parameters": {"fast_period": 10, "slow_period": 30},
        "position_policy": {
            "mode": "target_fraction",
            "long_only": True,
            "whole_shares": True,
            "allocation_fraction": "0.20",
            "rebalance": "on_signal_transition",
            "initial_entry": "wait_for_cross",
        },
        "execution_policy": {
            "decision": "completed_bar_available",
            "dispatch": "next_regular_session",
            "order_type": "market",
            "time_in_force": "DAY",
            "outside_rth": False,
            "gap_action": "reject_and_pause",
            "missed_session_action": "expire_and_pause",
        },
    }
    value.update(overrides)
    return value


def test_strategy_spec_freezes_supported_template_and_semantics():
    spec = StrategySpec.model_validate(make_strategy())
    assert spec.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE
    assert spec.position_policy.mode == "target_fraction"
    assert spec.position_policy.allocation_fraction == Decimal("0.20")
    assert canonical_sha256(spec) == canonical_sha256(StrategySpec.model_validate(make_strategy()))


@pytest.mark.parametrize(
    "change",
    [
        {"unsupported": True},
        {"bar_policy": {"timeframe": "1m", "session": "regular", "completed_only": True, "calendar_id": "x", "timezone": "UTC"}},
        {"bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": False, "calendar_id": "x", "timezone": "UTC"}},
        {"instrument_ids": ["A", "B"]},
        {"position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True}},
        {"execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session", "order_type": "limit", "time_in_force": "DAY", "outside_rth": False, "gap_action": "reject", "missed_session_action": "expire"}},
    ],
)
def test_strategy_spec_rejects_unknown_or_unsupported_contract_values(change):
    data = make_strategy()
    data.update(change)
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(data)


def test_strategy_spec_parameters_must_be_json_values():
    data = make_strategy()
    data["parameters"] = {"fast_period": object()}
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(data)


def test_explicit_quantity_cannot_be_recomputed_from_target_fraction():
    policy = PositionPolicy(mode="explicit_quantity", long_only=True, whole_shares=True)
    assert policy.allocation_fraction is None
    with pytest.raises(ValidationError):
        PositionPolicy(
            mode="explicit_quantity",
            long_only=True,
            whole_shares=True,
            allocation_fraction="0.5",
        )


def test_order_intent_uses_exact_positive_integer_and_aware_times():
    start = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    intent = OrderIntent(
        intent_id="i-1",
        decision_id="d-1",
        dedupe_key="account/deployment/version/seq/action",
        account_ref="paper-account-ref",
        environment=Environment.PAPER,
        side="BUY",
        integer_qty=3,
        order_type="market",
        time_in_force="DAY",
        reference_price="123.45",
        eligible_at=start,
        expires_at=start + timedelta(hours=7),
        risk_snapshot_id="risk-1",
    )
    assert intent.reference_price == Decimal("123.45")
    assert intent.eligible_at.tzinfo == timezone.utc
    with pytest.raises(ValidationError):
        OrderIntent(**{**intent.model_dump(), "integer_qty": 0})
    with pytest.raises(ValidationError):
        OrderIntent(**{**intent.model_dump(), "eligible_at": datetime(2026, 10, 20)})


def test_event_envelope_requires_aware_timestamps_and_normalizes_utc():
    stamp = datetime(2026, 10, 20, 10, tzinfo=timezone(timedelta(hours=-4)))
    event = EventEnvelope(
        event_id="e-1",
        seq=1,
        deployment_id="dep-1",
        run_id="run-1",
        account_ref="acct-ref",
        environment="paper",
        kind="completed_bar",
        occurred_at=stamp,
        received_at=stamp,
        persisted_at=stamp,
        payload={"close": "101.25"},
    )
    assert event.occurred_at.hour == 14
    with pytest.raises(ValidationError):
        EventEnvelope(**{**event.model_dump(), "seq": 0})
    with pytest.raises(ValidationError):
        EventEnvelope(**{**event.model_dump(), "persisted_at": stamp - timedelta(seconds=1)})


def test_canonical_json_orders_keys_and_preserves_decimal_exactness():
    left = {"b": Decimal("1.2300"), "a": {"z": 2, "x": 1}}
    right = {"a": {"x": 1, "z": 2}, "b": Decimal("1.2300")}
    assert canonical_json(left) == '{"a":{"x":1,"z":2},"b":"1.23"}'
    assert canonical_sha256(left) == canonical_sha256(right)
    assert canonical_sha256(left) == canonical_sha256({"a": {"z": 2, "x": 1}, "b": Decimal("1.23")})


def test_persisted_run_and_order_states_use_contract_values():
    assert RunState.RECONCILING.value == "RECONCILING"
    assert LocalSubmissionState.SUBMISSION_UNKNOWN.value == "SUBMISSION_UNKNOWN"
    assert BrokerOrderState.PARTIALLY_FILLED.value == "PARTIALLY_FILLED"


def test_canonical_json_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_risk_grant_requires_explicit_scope_hash_and_bounded_window():
    start = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    grant = RiskGrant(
        grant_id="grant-1",
        user_action_id="user-action-1",
        strategy_version_hash="a" * 64,
        broker="ibkr",
        account_ref="paper-account-ref",
        environment="paper",
        allowed_instruments=("us_equity_demo",),
        allowed_order_types=("market",),
        long_only=True,
        whole_shares=True,
        max_order_notional="500",
        max_gross_exposure="2000",
        strategy_budget="1500",
        max_daily_loss="100",
        max_orders_per_session=5,
        valid_from=start,
        valid_until=start + timedelta(days=1),
        quote_max_age_seconds=15,
        account_max_age_seconds=60,
        halt_action="pause_and_cancel_owned",
        liquidation_policy="none",
        granted_at=start,
    )
    assert grant.strategy_version_hash == "a" * 64
    assert grant.environment is Environment.PAPER
    with pytest.raises(ValidationError):
        RiskGrant(**{**grant.model_dump(), "strategy_version_hash": "not-a-hash"})
    with pytest.raises(ValidationError):
        RiskGrant(**{**grant.model_dump(), "valid_until": start})


def test_bar_and_manifest_validate_time_quality_ranges_and_synthetic_provenance():
    start = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
    bar = Bar(
        instrument_id="us_equity_demo",
        session_date=date(2026, 10, 19),
        start_at=start,
        end_at=end,
        available_at=end + timedelta(minutes=1),
        timezone="America/New_York",
        open="100.00",
        high="102.00",
        low="99.00",
        close="101.00",
        volume="1000",
        currency="USD",
        is_final=True,
        source="synthetic_fixture",
        adjustment="unadjusted",
    )
    assert bar.available_at == end + timedelta(minutes=1)
    with pytest.raises(ValidationError):
        Bar(**{**bar.model_dump(), "high": "100.50"})
    with pytest.raises(ValidationError):
        Bar(**{**bar.model_dump(), "available_at": end - timedelta(seconds=1)})

    manifest = DataManifest(
        manifest_id="synthetic-manifest-1",
        provider="synthetic_fixture",
        instrument_id="us_equity_demo",
        currency="USD",
        fields=("open", "high", "low", "close", "volume"),
        range_start=date(2026, 10, 1),
        range_end=date(2026, 10, 10),
        availability_model="bar end + 1 minute",
        calendar_version="synthetic-calendar-v1",
        adjustment_policy="unadjusted",
        corporate_action_policy="none; synthetic data",
        download_parameters={},
        fetched_at=end,
        file_sha256="b" * 64,
        quality_report={"valid": True},
        warmup_start=date(2026, 10, 1),
        warmup_end=date(2026, 10, 3),
        evaluation_start=date(2026, 10, 4),
        evaluation_end=date(2026, 10, 10),
        synthetic=True,
        synthetic_note="generated deterministic unit fixture",
    )
    assert manifest.synthetic is True
    with pytest.raises(ValidationError):
        DataManifest(**{**manifest.model_dump(), "synthetic_note": None})
