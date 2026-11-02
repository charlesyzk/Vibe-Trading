"""Prepare a PAPER order from locally verified frozen backtest evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from src.bt_runtime.report_artifact_integrity import load_verified_report
from src.execution.contracts import Environment, RiskGrant, is_sha256
from src.execution.contracts.models import StrictModel
from src.execution.ibkr_paper_risk_facts import PaperOrderPolicy, VerifiedIbkrContract
from src.execution.paper_strategy_candidates import (
    _read_json, _regular_directory, _regular_file, _verified_paper_import_attestation,
)
from src.execution.risk import ProposedOrder
from src.execution.versions import StrategyVersionStore


def suggest_paper_entry_window(now: datetime) -> tuple[datetime, datetime]:
    """Suggest the remainder of this session or the next regular session."""
    from src.bt_runtime.calendars import build_xnys_schedule

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must include a timezone")
    now = now.astimezone(timezone.utc)
    local_date = now.astimezone(ZoneInfo("America/New_York")).date()
    for session in build_xnys_schedule(local_date, local_date + timedelta(days=14)).sessions:
        # Leave time for a partner to review and deploy the suggested setup.
        if now + timedelta(minutes=5) < session.close_at:
            return max(now, session.open_at), session.close_at
    raise ValueError("no upcoming regular session is available")


def next_session_dispatch_close(bar_end: datetime, now: datetime) -> datetime:
    """Return the next session close only while a completed bar is actionable."""
    from src.bt_runtime.calendars import build_xnys_schedule

    for value in (bar_end, now):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("signal and dispatch times must include a timezone")
    bar_end, now = bar_end.astimezone(timezone.utc), now.astimezone(timezone.utc)
    zone = ZoneInfo("America/New_York")
    bar_day, today = bar_end.astimezone(zone).date(), now.astimezone(zone).date()
    if today <= bar_day:
        raise ValueError("signal is not from the immediately preceding session")
    sessions = build_xnys_schedule(bar_day, today).sessions
    if (len(sessions) != 2 or sessions[0].session_date != bar_day or sessions[0].close_at != bar_end
            or sessions[1].session_date != today or not sessions[1].open_at <= now < sessions[1].close_at):
        raise ValueError("signal is stale or outside the next regular session")
    return sessions[1].close_at


class PaperOrderSetup(StrictModel):
    """User-selected order limits and a bounded regular-session date range.

    This is a preview input, not a persisted grant or authority to trade.
    """

    valid_from: datetime
    valid_until: datetime
    max_order_notional: Decimal = Field(gt=0, le=Decimal("1000000"))
    max_gross_exposure: Decimal = Field(gt=0, le=Decimal("1000000"))
    strategy_budget: Decimal = Field(gt=0, le=Decimal("1000000"))
    max_daily_loss: Decimal = Field(gt=0, le=Decimal("1000000"))
    max_orders_per_session: int = Field(strict=True, ge=1, le=100)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("entry window timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def regular_session(self) -> "PaperOrderSetup":
        from src.bt_runtime.calendars import resolve_xnys_session

        if self.valid_until <= self.valid_from:
            raise ValueError("entry window end must be after start")
        zone = ZoneInfo("America/New_York")
        first_day, last_day = self.valid_from.astimezone(zone).date(), self.valid_until.astimezone(zone).date()
        if (last_day - first_day).days > 90:
            raise ValueError("entry window cannot exceed 90 calendar days")
        first, last = resolve_xnys_session(first_day), resolve_xnys_session(last_day)
        if (first is None or last is None or not first.open_at <= self.valid_from < first.close_at
                or not last.open_at < self.valid_until <= last.close_at):
            raise ValueError("entry window endpoints must fall within XNYS regular sessions")
        return self

    def validate_order(self, order: ProposedOrder) -> None:
        if order.order_type == "limit":
            notional = Decimal(order.integer_qty) * order.limit_price
            if notional > self.max_order_notional:
                raise ValueError("limit order exceeds maximum order notional")
            if order.side == "BUY" and notional > min(self.strategy_budget, self.max_gross_exposure):
                raise ValueError("limit order exceeds strategy budget or gross exposure limit")


@dataclass(frozen=True, slots=True)
class PreparedPaperOrder:
    policy: PaperOrderPolicy
    contract: VerifiedIbkrContract
    report_sha256: str
    data_manifest_hash: str
    run_id: str

    def build_risk_grant(
        self, setup: PaperOrderSetup, *, grant_id: str, user_action_id: str, granted_at: datetime,
    ) -> RiskGrant:
        """Bind a validated setup to this exact evidence and PAPER account.

        Constructing the value does not persist authorization or open a gate;
        callers must use an attributable, audited deployment workflow.
        """
        if not isinstance(setup, PaperOrderSetup):
            raise TypeError("a validated PAPER order setup is required")
        for label, value in (("grant_id", grant_id), ("user_action_id", user_action_id)):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"{label} must identify the attributable authorization")
        setup.validate_order(self.policy.order)
        return RiskGrant(
            grant_id=grant_id, user_action_id=user_action_id,
            strategy_version_hash=self.policy.strategy_version_hash,
            broker="IBKR", account_ref=self.policy.account_ref, environment=Environment.PAPER,
            allowed_instruments=(self.policy.order.instrument_id,),
            allowed_order_types=(self.policy.order.order_type,), long_only=True, whole_shares=True,
            max_order_notional=setup.max_order_notional, max_gross_exposure=setup.max_gross_exposure,
            strategy_budget=setup.strategy_budget, max_daily_loss=setup.max_daily_loss,
            max_orders_per_session=setup.max_orders_per_session,
            valid_from=setup.valid_from, valid_until=setup.valid_until,
            quote_max_age_seconds=10, account_max_age_seconds=10,
            funds_check="account_cash", halt_action="pause_and_cancel_owned", liquidation_policy="none",
            granted_at=granted_at,
        )


def prepare_verified_paper_order(
    *, runtime_root: str | Path, run_id: str, version_id: str,
    account_ref: str, order: ProposedOrder,
) -> PreparedPaperOrder:
    """Resolve exact local evidence; this operation never connects or sends."""
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
        raise ValueError("invalid backtest run ID")
    if not isinstance(version_id, str) or not is_sha256(version_id):
        raise ValueError("invalid frozen strategy version")
    root = Path(runtime_root).expanduser()
    run = root / "runs" / run_id
    artifacts = run / "artifacts"
    report_path = artifacts / "execution_report.json"
    sidecar = artifacts / "execution_report.integrity.json"
    if not all((_regular_directory(root / "runs"), _regular_directory(run), _regular_directory(artifacts),
                _regular_file(report_path), _regular_file(sidecar))):
        raise ValueError("backtest evidence is unavailable")
    state, config = _read_json(run / "state.json"), _read_json(run / "config.json")
    if state.get("status") != "success" or config.get("execution_backend") != "backtrader":
        raise ValueError("backtest is not a successful Backtrader run")
    report, error = load_verified_report(report_path)
    if report is None or error:
        raise ValueError("backtest report integrity verification failed")
    report_bytes = report_path.read_bytes()
    sidecar_record = _read_json(sidecar)
    report_digest = hashlib.sha256(report_bytes).hexdigest()
    if (sidecar_record.get("sha256") != report_digest
            or sidecar_record.get("size_bytes") != len(report_bytes)
            or json.loads(report_bytes) != report):
        raise ValueError("backtest report changed during preparation")
    version = StrategyVersionStore(root / "strategy-versions").get_version(version_id)
    integrity = report.get("execution_integrity")
    provenance = report.get("provenance")
    digest = report.get("data_manifest_hash")
    if not (
        report.get("execution_backend") == "backtrader" and report.get("execution_mode") == "backtest"
        and report.get("strategy_version_hash") == version_id
        and report.get("strategy_id") == version.strategy_id
        and report.get("source_kind") == "python_upload"
        and report.get("instrument_id") == order.instrument_id
        and report.get("engine") == f"Backtrader {version.engine_version.removeprefix('backtrader/')} / BackBroker"
        and isinstance(provenance, dict) and provenance.get("strategy_version_hash") == version_id
        and isinstance(digest, str) and is_sha256(digest)
        and isinstance(integrity, dict) and integrity.get("status") == "verified"
        and integrity.get("data_manifest_hash") == digest
        and type(report.get("evaluation_bar_count")) is int and report["evaluation_bar_count"] > 0
    ):
        raise ValueError("backtest report differs from the frozen strategy or lacks verified replay")
    if not _verified_paper_import_attestation(config.get("ibkr_paper_import_attestation"), report):
        raise ValueError("backtest lacks verified IBKR PAPER market data and contract evidence")
    raw_contract = report["data_manifest"]["download_parameters"]["qualified_contract"]
    exchange = raw_contract.get("exchange")
    primary = raw_contract.get("primary_exchange")
    if exchange != "SMART" or not isinstance(primary, str) or not primary.strip():
        raise ValueError("qualified stock contract requires SMART and a primary exchange")
    policy = PaperOrderPolicy.from_frozen_version(account_ref=account_ref, version=version, order=order)
    contract = VerifiedIbkrContract(
        instrument_id=order.instrument_id, symbol=raw_contract["symbol"].upper(),
        con_id=raw_contract["con_id"], primary_exchange=primary.strip(),
    )
    return PreparedPaperOrder(
        policy=policy, contract=contract, report_sha256=report_digest,
        data_manifest_hash=digest, run_id=run_id,
    )
