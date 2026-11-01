"""Backtest the frozen one-shot AAPL PAPER strategy on current IBKR history."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import backtrader
import pandas as pd

from src.bt_runtime.market_data import canonical_daily_bars_sha256
from src.bt_runtime.strategies.python_upload import PythonBacktestReport, run_python_upload_backtest
from src.config.paths import get_runtime_root
from src.execution.contracts import DataManifest
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.ibkr_completed_bar_source import IbkrPaperCompletedBarSource
from src.execution.ibkr_paper_risk_facts import VerifiedIbkrContract
from src.execution.paper_integration_test_config import read_paper_integration_test_config
from src.execution.paper_backtest_attestation import write_paper_backtest_attestation
from src.execution.python_sandbox import PythonSandbox
from src.execution.versions import StrategyVersionStore
from src.trading.connectors.ibkr.local import load_config


@dataclass(frozen=True, slots=True)
class PaperIntegrationTestBacktestResult:
    report: PythonBacktestReport
    artifact_path: Path


class PaperIntegrationTestBacktestError(RuntimeError):
    """The frozen test strategy could not be backtested on trusted IBKR bars."""


def _operator_setting(name: str) -> str:
    """Read one explicit worker setting without importing unrelated dotenv values."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = get_runtime_root() / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#") and "=" in clean:
                key, raw = clean.split("=", 1)
                if key.strip() == name:
                    return raw.strip().strip("\"'")
    except OSError:
        pass
    return ""


def run_paper_integration_test_backtest(*, duration: str = "1 M") -> PaperIntegrationTestBacktestResult:
    """Run a read-only data request and isolated frozen-source Backtrader replay."""
    config = read_paper_integration_test_config()
    if not config.strategy_version_id:
        raise PaperIntegrationTestBacktestError("integration-test config has no frozen strategy version")
    versions = StrategyVersionStore(get_runtime_root() / "strategy-versions")
    try:
        version = versions.get_version(config.strategy_version_id)
        if version.engine_version != f"backtrader/{backtrader.__version__}" or (
            version.compiler_version != PYTHON_WORKER_COMPILER_VERSION
        ):
            raise ValueError("frozen strategy runtime provenance differs from the installed worker")
        local_config = load_config()
        if (
            local_config.profile != "paper"
            or local_config.port != config.endpoint.port
            or local_config.account != config.account_ref
            or local_config.readonly is not True
        ):
            raise ValueError("local IBKR profile differs from the fixed PAPER test account")
        records = IbkrPaperCompletedBarSource(
            config=local_config,
            account_ref=config.account_ref,
            contract=VerifiedIbkrContract(
                instrument_id="AAPL",
                symbol="AAPL",
                con_id=config.instrument.verified_con_id,
                primary_exchange="NASDAQ",
            ),
            duration=duration,
        )()
        if len(records) < 3:
            raise ValueError("IBKR history must contain at least two warmup bars and the pinned signal bar")
        frame = pd.DataFrame(
            [
                {field: float(getattr(bar, field)) for field in ("open", "high", "low", "close", "volume")}
                for bar in records
            ],
            index=pd.DatetimeIndex([bar.available_at for bar in records], name="available_at"),
        )
        source_bytes = json.dumps(
            [bar.model_dump(mode="json") for bar in records],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(source_bytes).hexdigest()
        manifest = DataManifest(
            manifest_id=f"ibkr-paper-aapl-{digest[:16]}",
            provider="IBKR PAPER Gateway historical TRADES",
            instrument_id="AAPL",
            currency="USD",
            fields=("session_date", "start_at", "end_at", "available_at", "open", "high", "low", "close", "volume"),
            range_start=records[0].session_date,
            range_end=records[-1].session_date,
            availability_model=(
                "completed XNYS daily bar at session close; point-in-time historical publication unverified"
            ),
            calendar_version="XNYS@4.13.2",
            adjustment_policy="IBKR historical adjustment semantics unverified",
            corporate_action_policy="IBKR corporate-action adjustment semantics unverified",
            download_parameters={
                "duration": duration,
                "bar_size": "1 day",
                "what_to_show": "TRADES",
                "use_rth": True,
                "contract_id": config.instrument.verified_con_id,
            },
            fetched_at=datetime.now(timezone.utc),
            file_sha256=digest,
            quality_report={
                "validation": "passed",
                "session_schedule": "exact_match",
                "strategy_input_schema": "vibe-daily-ohlcv/1",
                "strategy_input_sha256": canonical_daily_bars_sha256(frame),
                "provider_attestation": "local IBKR PAPER Gateway response; vendor attestation unavailable",
                "point_in_time_availability": "unverified",
                "adjustment_attestation": "unverified",
                "bar_quality_flags": sorted({flag for bar in records for flag in bar.quality_flags}),
            },
            warmup_start=records[0].session_date,
            warmup_end=records[-2].session_date,
            evaluation_start=records[-1].session_date,
            evaluation_end=records[-1].session_date,
            synthetic=False,
        )
        image = _operator_setting("VIBE_PYTHON_WORKER_IMAGE")
        if not image:
            raise ValueError("VIBE_PYTHON_WORKER_IMAGE is not configured")
        context = _operator_setting("DOCKER_CONTEXT") or None
        sandbox = PythonSandbox(image, context=context)
        report = run_python_upload_backtest(
            version.spec,
            frame,
            manifest,
            version_id=version.version_id,
            versions=versions,
            sandbox=sandbox,
            bar_records=records,
            initial_cash=1_000.0,
            commission_rate=0.001,
        )
        output_dir = get_runtime_root() / "paper-integration-test" / "backtests"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"{stamp}-{version.version_id}.json"
        fd, temporary_name = tempfile.mkstemp(prefix=".backtest.", dir=output_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(report.to_dict(), stream, ensure_ascii=False, sort_keys=True, indent=2, default=str)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary_name, output_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        write_paper_backtest_attestation(output_path, version_id=version.version_id)
        return PaperIntegrationTestBacktestResult(report=report, artifact_path=output_path)
    except PaperIntegrationTestBacktestError:
        raise
    except Exception as exc:
        raise PaperIntegrationTestBacktestError("frozen PAPER integration-test backtest failed") from exc


__all__ = [
    "PaperIntegrationTestBacktestError",
    "PaperIntegrationTestBacktestResult",
    "run_paper_integration_test_backtest",
]
