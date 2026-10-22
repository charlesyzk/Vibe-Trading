"""Trusted composition seam for immutable Python versions and imported bars.

This service intentionally accepts ``ImportedMarketData`` and the original
CSV bytes, not legacy provider DataFrames. It persists the exact input and
version evidence beside a Backtrader-generated report.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.bt_runtime.market_data import ImportedMarketData
from src.bt_runtime.backtest_event_journal import SQLiteBacktestEventJournal
from src.bt_runtime.strategies.python_upload import run_python_upload_backtest
from src.execution.contracts import SourceKind, canonical_json, canonical_sha256
from src.execution.event_worker import PythonBacktestEventCoordinator
from src.execution.python_sandbox import PythonSandbox
from src.execution.versions import StrategyVersionStore


def run_python_upload_service(
    *,
    version_id: str,
    versions: StrategyVersionStore,
    market_data: ImportedMarketData,
    raw_csv: bytes,
    sandbox: PythonSandbox,
    run_dir: str | Path,
    initial_cash: float = 100_000.0,
    commission_rate: float = 0.001,
    slippage_fraction: float = 0.0,
    run_id: str | None = None,
    resume_context: dict[str, Any] | None = None,
    worker_image: str | None = None,
) -> dict[str, Any]:
    """Run and persist one isolated Python-upload Backtrader backtest.

    The CSV hash is checked against both importer output and DataManifest so
    the saved artifact is exactly the byte stream that produced the Bar rows.
    """
    if not isinstance(raw_csv, bytes) or not raw_csv:
        raise ValueError("original imported market-data CSV bytes are required")
    raw_hash = hashlib.sha256(raw_csv).hexdigest()
    if raw_hash != market_data.raw_sha256 or raw_hash != market_data.manifest.file_sha256:
        raise ValueError("raw market-data bytes do not match ImportedMarketData manifest")
    version = versions.get_version(version_id)
    if version.source_kind is not SourceKind.PYTHON_UPLOAD:
        raise ValueError("Python upload service requires a frozen Python version")
    if market_data.manifest.instrument_id != version.spec.instrument_ids[0]:
        raise ValueError("imported market data instrument does not match the frozen strategy")
    run_path = Path(run_dir)
    resolved_run_id = run_id or run_path.name
    if not resolved_run_id or resolved_run_id != run_path.name:
        raise ValueError("backtest run_id must match the persisted run directory")
    artifacts = run_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    resume_metadata = resume_context or _derive_resume_context(market_data)
    run_config = {
        "run_id": resolved_run_id,
        "execution_mode": "backtest",
        "strategy_version_hash": version.version_hash,
        "strategy_spec_hash": version.spec_hash,
        "engine_version": version.engine_version,
        "worker_protocol_version": version.compiler_version,
        "worker_image": worker_image,
        "data_manifest_hash": canonical_sha256(market_data.manifest),
        "raw_market_data_sha256": raw_hash,
        "initial_cash": float(initial_cash),
        "commission_rate": float(commission_rate),
        "slippage_fraction": float(slippage_fraction),
    }
    config_hash = canonical_sha256(run_config)
    run_config["run_config_hash"] = config_hash
    _persist_immutable(artifacts / "market_data.csv", raw_csv)
    _persist_immutable(artifacts / "data_manifest.json", canonical_json(market_data.manifest).encode("utf-8"))
    _persist_immutable(artifacts / "strategy_version.json", canonical_json(version).encode("utf-8"))
    _persist_immutable(
        artifacts / "verified_bars.json",
        canonical_json(market_data.bar_records).encode("utf-8"),
    )
    _persist_immutable(artifacts / "backtest_input.json", canonical_json(resume_metadata).encode("utf-8"))
    _persist_immutable(
        artifacts / "backtest_run_config.json",
        json.dumps(run_config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
    )

    with _exclusive_run_lock(artifacts / "backtest.lock"):
        journal = SQLiteBacktestEventJournal(artifacts / "backtest_events.sqlite3")
        journal.bind_run(
            run_id=resolved_run_id,
            strategy_version_hash=version.version_hash,
            strategy_spec_hash=version.spec_hash,
            run_config_hash=config_hash,
        )
        coordinator = PythonBacktestEventCoordinator(
            journal=journal,
            run_id=resolved_run_id,
            source=versions.read_upload_source(version.version_id).decode("utf-8", errors="strict"),
            sandbox=sandbox,
            released_bars=market_data.bar_records,
        )
        report = run_python_upload_backtest(
            version.spec,
            market_data.bars,
            market_data.manifest,
            version_id=version.version_id,
            versions=versions,
            sandbox=sandbox,
            bar_records=market_data.bar_records,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            slippage_fraction=slippage_fraction,
            event_coordinator=coordinator,
        ).to_dict()
        report.update({
            "execution_backend": "backtrader",
            "execution_mode": "backtest",
            "strategy_version_id": version.version_id,
            "strategy_version_hash": version.version_hash,
            "strategy_spec_hash": version.spec_hash,
            "mode": "isolated_python_upload_with_imported_market_data",
            "worker_image": worker_image,
        })
    _atomic_write(
        artifacts / "execution_report.json",
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
    )
    from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar

    write_report_integrity_sidecar(artifacts / "execution_report.json")
    return report


def load_python_upload_resume_inputs(run_dir: str | Path) -> dict[str, Any]:
    """Load persisted, exact inputs for resuming a failed/running backtest."""
    from datetime import date, datetime
    from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv

    run_path = Path(run_dir)
    artifacts = run_path / "artifacts"
    config = json.loads((artifacts / "backtest_run_config.json").read_text(encoding="utf-8"))
    saved_input = json.loads((artifacts / "backtest_input.json").read_text(encoding="utf-8"))
    version_record = json.loads((artifacts / "strategy_version.json").read_text(encoding="utf-8"))
    version_id = version_record.get("version_id")
    if config.get("run_id") != run_path.name or config.get("execution_mode") != "backtest":
        raise ValueError("persisted run is not a resumable Python backtest")
    if version_id != config.get("strategy_version_hash"):
        raise ValueError("persisted strategy version does not match the run configuration")
    metadata = MarketDataMetadata.model_validate(saved_input["market_data_metadata"])
    sessions = [
        MarketSession(
            session_date=date.fromisoformat(value["session_date"]),
            open_at=datetime.fromisoformat(value["open_at"].replace("Z", "+00:00")),
            close_at=datetime.fromisoformat(value["close_at"].replace("Z", "+00:00")),
            early_close=bool(value.get("early_close", False)),
        )
        for value in saved_input["expected_sessions"]
    ]
    raw_csv = (artifacts / "market_data.csv").read_bytes()
    market_data = import_completed_daily_csv(raw_csv, metadata, sessions)
    if canonical_sha256(market_data.manifest) != config.get("data_manifest_hash"):
        raise ValueError("persisted market data no longer matches the original manifest")
    verified_bars = (artifacts / "verified_bars.json").read_bytes()
    if verified_bars != canonical_json(market_data.bar_records).encode("utf-8"):
        raise ValueError("persisted verified Bar records do not match the imported market data")
    return {
        "run_id": run_path.name,
        "version_id": version_id,
        "market_data": market_data,
        "raw_csv": raw_csv,
        "resume_context": saved_input,
        "initial_cash": float(config["initial_cash"]),
        "commission_rate": float(config["commission_rate"]),
        "slippage_fraction": float(config.get("slippage_fraction", 0.0)),
        "strategy_version_hash": config["strategy_version_hash"],
        "strategy_spec_hash": config["strategy_spec_hash"],
    }


@contextmanager
def _exclusive_run_lock(path: Path):
    with path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("this Backtrader backtest is already running or resuming") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _derive_resume_context(market_data: ImportedMarketData) -> dict[str, Any]:
    manifest = market_data.manifest
    calendar_id = str(manifest.quality_report.get("calendar_provider", "unknown_calendar"))
    calendar_version = manifest.calendar_version.split("@", 1)[-1]
    timezone_name = market_data.bar_records[0].timezone
    early_closes = set(manifest.quality_report.get("early_close_sessions", []))
    metadata = {
        "provider": manifest.provider,
        "instrument_id": manifest.instrument_id,
        "currency": market_data.bar_records[0].currency,
        "timezone": timezone_name,
        "calendar_id": calendar_id,
        "calendar_version": calendar_version,
        "availability_model": manifest.availability_model,
        "adjustment_policy": manifest.adjustment_policy,
        "corporate_action_policy": manifest.corporate_action_policy,
        "fetched_at": manifest.fetched_at.isoformat(),
        "warmup_start": manifest.warmup_start.isoformat(),
        "warmup_end": manifest.warmup_end.isoformat(),
        "evaluation_start": manifest.evaluation_start.isoformat(),
        "evaluation_end": manifest.evaluation_end.isoformat(),
        "download_parameters": manifest.download_parameters,
    }
    sessions = [
        {
            "session_date": bar.session_date.isoformat(),
            "open_at": bar.start_at.isoformat(),
            "close_at": bar.end_at.isoformat(),
            "early_close": bar.session_date.isoformat() in early_closes,
        }
        for bar in market_data.bar_records
    ]
    return {"market_data_metadata": metadata, "expected_sessions": sessions}


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one artifact atomically within its run directory."""
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _persist_immutable(path: Path, content: bytes) -> None:
    """Write exact run inputs once; retries must prove byte-for-byte identity."""
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"persisted backtest input changed: {path.name}")
        return
    _atomic_write(path, content)
