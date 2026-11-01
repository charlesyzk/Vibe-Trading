from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.execution.paper_integration_test_config import (
    PaperIntegrationTestConfig,
    PaperIntegrationTestConfigError,
    read_paper_integration_test_config,
)


def _payload() -> dict:
    return {
        "schema_version": "1.0",
        "mode": "integration-test",
        "enabled": False,
        "broker": "IBKR",
        "environment": "paper",
        "account_ref": "DUR123456",
        "endpoint": {"host": "127.0.0.1", "port": 4002, "profile": "ib_gateway_paper"},
        "instrument": {
            "symbol": "AAPL", "security_type": "STK", "exchange": "SMART",
            "currency": "USD", "verified_con_id": 265598,
        },
        "order": {
            "side": "BUY", "quantity": 1, "order_type": "LMT", "time_in_force": "DAY",
            "outside_rth": False, "limit_price": "400.00",
        },
        "risk": {
            "max_order_notional_usd": "400", "strategy_budget_usd": "400",
            "max_gross_exposure_usd": "400", "max_daily_loss_usd": "400",
            "max_orders_per_session": 1, "price_buffer_fraction": "0.05",
            "estimated_fee_reserve_usd": "1.00", "quote_max_age_seconds": 10,
            "account_max_age_seconds": 10, "funds_check": "broker_paper_limit",
        },
        "valid_window_utc": {"start": "2026-11-02T14:30:00Z", "end": "2026-11-02T21:00:00Z"},
        "submission_policy": "explicit_operator_action_only",
        "max_notional_semantics": (
            "hard limit price ceiling; estimated shares notional is capped at the limit price and IBKR cannot execute above it"
        ),
        "prepared_at": "2026-10-31T12:29:20.892951+00:00",
    }


def test_config_pins_one_paper_aapl_limit_order_and_stays_disabled_by_default():
    config = PaperIntegrationTestConfig.model_validate(_payload())

    assert config.enabled is False
    assert config.environment == "paper"
    assert config.endpoint.port == 4002
    assert config.instrument.verified_con_id == 265598
    assert config.order.quantity == 1
    assert str(config.order.limit_price) == "400.00"
    assert config.is_submission_window_open(config.valid_window_utc.start) is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("environment",), "live"),
        (("enabled",), "false"),
        (("endpoint", "port"), 4001),
        (("instrument", "verified_con_id"), 265599),
        (("order", "quantity"), 2),
        (("order", "limit_price"), "401.00"),
        (("risk", "max_order_notional_usd"), "401"),
        (("risk", "max_orders_per_session"), 2),
        (("valid_window_utc", "start"), "2026-11-02T22:30:00+08:00"),
    ],
)
def test_config_rejects_scope_or_bound_changes(path, value):
    payload = _payload()
    target = payload
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        PaperIntegrationTestConfig.model_validate(payload)


def test_config_window_is_half_open_and_requires_explicit_enablement():
    payload = _payload()
    payload["enabled"] = True
    config = PaperIntegrationTestConfig.model_validate(payload)
    start, end = config.valid_window_utc.start, config.valid_window_utc.end

    assert config.is_submission_window_open(start)
    assert config.is_submission_window_open(end.replace(microsecond=0)) is False
    assert config.is_submission_window_open(start.replace(microsecond=0))
    assert config.is_submission_window_open(start.replace(year=2026, day=1)) is False


def test_reader_rejects_symlinked_config(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_payload()), encoding="utf-8")
    link = tmp_path / "paper-integration-test.json"
    link.symlink_to(target)

    with pytest.raises(PaperIntegrationTestConfigError, match="non-symlink"):
        read_paper_integration_test_config(link)


def test_reader_loads_valid_regular_file_without_writing_it(tmp_path: Path):
    path = tmp_path / "paper-integration-test.json"
    content = json.dumps(_payload(), indent=2) + "\n"
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()

    config = read_paper_integration_test_config(path)

    assert config.account_ref == "DUR123456"
    assert path.read_bytes() == before
