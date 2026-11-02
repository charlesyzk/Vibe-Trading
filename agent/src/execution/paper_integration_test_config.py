"""Strict reader for the operator-prepared one-shot IBKR PAPER test order.

The file grants no authority by itself. It pins the exact bounded request;
submission still requires an explicit attributable operator action and the
managed PAPER runtime's ordinary ledger, risk, callback, and reconciliation
path. This module is deliberately read-only.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from src.config.paths import get_runtime_root
from src.execution.contracts.models import StrictModel, is_sha256


CONFIG_FILENAME = "paper-integration-test.json"
MAX_CONFIG_BYTES = 16 * 1024
_PAPER_ACCOUNT_RE = re.compile(r"^DU[A-Z0-9]+$")


class PaperIntegrationTestConfigError(ValueError):
    """The local one-shot test order configuration is unsafe or malformed."""


class IntegrationTestEndpoint(StrictModel):
    host: Literal["127.0.0.1", "localhost", "::1"]
    port: Literal[4002]
    profile: Literal["ib_gateway_paper"]


class IntegrationTestInstrument(StrictModel):
    symbol: Literal["AAPL"]
    security_type: Literal["STK"]
    exchange: Literal["SMART"]
    currency: Literal["USD"]
    verified_con_id: Literal[265598]


class IntegrationTestOrder(StrictModel):
    side: Literal["BUY"]
    quantity: int = Field(strict=True, ge=1, le=1)
    order_type: Literal["LMT"]
    time_in_force: Literal["DAY"]
    outside_rth: Literal[False]
    limit_price: Decimal

    @field_validator("limit_price")
    @classmethod
    def finite_positive_limit(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value != Decimal("400.00"):
            raise ValueError("integration-test AAPL limit must equal the configured $400.00 ceiling")
        return value


class IntegrationTestRisk(StrictModel):
    max_order_notional_usd: Decimal
    strategy_budget_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_daily_loss_usd: Decimal
    max_orders_per_session: int = Field(strict=True, ge=1, le=1)
    price_buffer_fraction: Decimal
    estimated_fee_reserve_usd: Decimal
    quote_max_age_seconds: int = Field(strict=True, ge=1, le=30)
    account_max_age_seconds: int = Field(strict=True, ge=1, le=30)
    funds_check: Literal["broker_paper_limit"]

    @field_validator(
        "max_order_notional_usd", "strategy_budget_usd", "max_gross_exposure_usd",
        "max_daily_loss_usd", "estimated_fee_reserve_usd",
    )
    @classmethod
    def finite_nonnegative_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("risk amounts must be finite and non-negative")
        return value

    @field_validator("price_buffer_fraction")
    @classmethod
    def finite_nonnegative_buffer(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
            raise ValueError("price_buffer_fraction must be between 0 and 1")
        return value


class IntegrationTestWindow(StrictModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def utc_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("integration-test window must use timezone-aware UTC timestamps")
        if value.utcoffset() != timedelta(0):
            raise ValueError("integration-test window timestamps must be stated in UTC")
        value = value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def ordered_window(self) -> "IntegrationTestWindow":
        if self.end <= self.start:
            raise ValueError("integration-test window end must be after start")
        return self


class PaperIntegrationTestConfig(StrictModel):
    schema_version: Literal["1.0"]
    mode: Literal["integration-test"]
    enabled: StrictBool
    broker: Literal["IBKR"]
    environment: Literal["paper"]
    account_ref: str
    strategy_version_id: str | None = None
    backtest_report_sha256: str | None = None
    endpoint: IntegrationTestEndpoint
    instrument: IntegrationTestInstrument
    order: IntegrationTestOrder
    risk: IntegrationTestRisk
    valid_window_utc: IntegrationTestWindow
    submission_policy: Literal["explicit_operator_action_only"]
    max_notional_semantics: Literal[
        "hard limit price ceiling; estimated shares notional is capped at the limit price and IBKR cannot execute above it"
    ]
    prepared_at: datetime

    @field_validator("account_ref")
    @classmethod
    def paper_account_shape(cls, value: str) -> str:
        if not isinstance(value, str) or not _PAPER_ACCOUNT_RE.fullmatch(value):
            raise ValueError("account_ref must be one explicit IBKR DU PAPER account")
        return value

    @field_validator("strategy_version_id", "backtest_report_sha256")
    @classmethod
    def version_id_is_immutable_hash(cls, value: str | None) -> str | None:
        if value is not None and not is_sha256(value):
            raise ValueError("strategy version and backtest report IDs must be lowercase SHA-256 hashes")
        return value

    @field_validator("prepared_at")
    @classmethod
    def prepared_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prepared_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def caps_cover_exact_order(self) -> "PaperIntegrationTestConfig":
        if self.backtest_report_sha256 is not None and self.strategy_version_id is None:
            raise ValueError("a pinned backtest report requires a strategy version")
        notional = Decimal(self.order.quantity) * self.order.limit_price
        risk = self.risk
        if notional > risk.max_order_notional_usd:
            raise ValueError("configured limit order exceeds max_order_notional_usd")
        if notional > risk.strategy_budget_usd or notional > risk.max_gross_exposure_usd:
            raise ValueError("configured limit order exceeds strategy or gross exposure budget")
        if (
            risk.max_order_notional_usd != risk.strategy_budget_usd
            or risk.max_order_notional_usd != risk.max_gross_exposure_usd
        ):
            raise ValueError("integration-test order, strategy, and gross caps must match exactly")
        if (
            risk.max_order_notional_usd != Decimal("400")
            or risk.max_daily_loss_usd != Decimal("400")
            or risk.price_buffer_fraction != Decimal("0.05")
            or risk.estimated_fee_reserve_usd != Decimal("1.00")
            or risk.quote_max_age_seconds != 10
            or risk.account_max_age_seconds != 10
            or risk.funds_check != "broker_paper_limit"
        ):
            raise ValueError("integration-test risk settings differ from the explicitly configured envelope")
        if (
            self.valid_window_utc.start != datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
            or self.valid_window_utc.end != datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
        ):
            raise ValueError("integration-test window differs from the explicitly configured session")
        return self

    def is_submission_window_open(self, now: datetime) -> bool:
        """Whether the configured explicit submission window currently allows dispatch."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("submission-window check requires a timezone-aware timestamp")
        instant = now.astimezone(timezone.utc)
        return self.enabled and self.valid_window_utc.start <= instant < self.valid_window_utc.end


def read_paper_integration_test_config(
    path: str | Path | None = None,
) -> PaperIntegrationTestConfig:
    """Read and validate the explicit user-level test order configuration."""
    config_path = Path(path).expanduser() if path is not None else get_runtime_root() / CONFIG_FILENAME
    try:
        info = config_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PaperIntegrationTestConfigError("integration-test config must be a regular non-symlink file")
        if info.st_size <= 0 or info.st_size > MAX_CONFIG_BYTES:
            raise PaperIntegrationTestConfigError("integration-test config has an invalid byte size")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return PaperIntegrationTestConfig.model_validate(payload)
    except PaperIntegrationTestConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PaperIntegrationTestConfigError("integration-test config is missing or invalid") from exc


def write_paper_integration_test_config(
    config: PaperIntegrationTestConfig,
    path: str | Path | None = None,
) -> Path:
    """Atomically persist a validated local config with owner-only permissions."""
    if not isinstance(config, PaperIntegrationTestConfig):
        raise TypeError("config must be a validated PaperIntegrationTestConfig")
    target = Path(path).expanduser() if path is not None else get_runtime_root() / CONFIG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PaperIntegrationTestConfigError("integration-test config target must be a regular non-symlink file")
    encoded = (json.dumps(config.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode("utf-8")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".paper-integration-test.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, target)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target


__all__ = [
    "CONFIG_FILENAME",
    "IntegrationTestEndpoint",
    "IntegrationTestInstrument",
    "IntegrationTestOrder",
    "IntegrationTestRisk",
    "IntegrationTestWindow",
    "MAX_CONFIG_BYTES",
    "PaperIntegrationTestConfig",
    "PaperIntegrationTestConfigError",
    "read_paper_integration_test_config",
    "write_paper_integration_test_config",
]
