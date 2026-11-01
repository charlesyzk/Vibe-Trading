"""Read-only catalog of frozen strategy versions with verified Backtrader runs."""

from __future__ import annotations

import hashlib
import json
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.bt_runtime.report_artifact_integrity import load_verified_report
from src.config.paths import get_runtime_root
from src.execution.paper_backtest_attestation import (
    PaperBacktestAttestationError,
    verify_paper_backtest_attestation,
)
from src.execution.paper_integration_test_config import read_paper_integration_test_config
from src.execution.versions import StrategyVersionStore


MAX_SCANNED_RUNS = 2_000


def list_paper_strategy_candidates(*, runtime_root: str | Path | None = None) -> dict[str, Any]:
    """List frozen versions with intact successful Backtrader reports.

    Deployable Python uploads must carry the server-issued host-observed
    IBKR PAPER import proof into the exact backtest report. Other versions
    remain visible with a concrete block reason so an offline report or caller
    supplied provider label is never mistaken for PAPER readiness.
    """
    root = Path(runtime_root).expanduser() if runtime_root is not None else get_runtime_root()
    versions = StrategyVersionStore(root / "strategy-versions")
    try:
        config = read_paper_integration_test_config(root / "paper-integration-test.json")
        selected_version_id = config.strategy_version_id
    except Exception:
        selected_version_id = None

    candidates: dict[str, dict[str, Any]] = {}
    runs_dir = root / "runs"
    for run_dir in _run_directories(runs_dir):
        try:
            state = _read_json(run_dir / "state.json")
            config_data = _read_json(run_dir / "config.json")
            if (
                state.get("status") != "success"
                or config_data.get("execution_backend") != "backtrader"
            ):
                continue
            report_path = run_dir / "artifacts" / "execution_report.json"
            sidecar_path = report_path.with_name("execution_report.integrity.json")
            if not _regular_directory(run_dir / "artifacts") or not _regular_file(report_path) or not _regular_file(sidecar_path):
                continue
            report, report_error = load_verified_report(report_path)
            if report is None or report_error:
                continue
            if (
                report.get("execution_backend") != "backtrader"
                or report.get("execution_mode") != "backtest"
            ):
                continue
            version_id = report.get("strategy_version_id") or report.get("strategy_version_hash")
            if not isinstance(version_id, str):
                continue
            version = versions.get_version(version_id)
            integrity = report.get("execution_integrity")
            manifest_hash = report.get("data_manifest_hash")
            if (
                report.get("strategy_version_hash") != version_id
                or report.get("strategy_id") != version.strategy_id
                or report.get("instrument_id") not in version.spec.instrument_ids
                or report.get("source_kind") != version.source_kind.value
                or not isinstance(integrity, dict)
                or integrity.get("status") != "verified"
                or integrity.get("data_manifest_hash") != manifest_hash
            ):
                continue
            raw = report_path.read_bytes()
            item = _candidate(
                version=version,
                run_id=run_dir.name,
                report_sha256=hashlib.sha256(raw).hexdigest(),
                report=report,
                provider_attestation=config_data.get("ibkr_paper_import_attestation"),
                selected_version_id=selected_version_id,
                one_shot_verified=False,
            )
            previous = candidates.get(version_id)
            if previous is None or item["run_id"] > previous["run_id"]:
                candidates[version_id] = item
        except Exception:
            # A single stale/corrupt local run cannot hide other valid versions.
            continue

    if selected_version_id:
        try:
            version = versions.get_version(selected_version_id)
            attestation = verify_paper_backtest_attestation(
                root / "paper-integration-test" / "backtests",
                version=version,
            )
            report = json.loads(attestation.report_path.read_text(encoding="utf-8"))
            candidates[selected_version_id] = _candidate(
                version=version,
                run_id="paper-integration-test",
                report_sha256=attestation.report_sha256,
                report=report,
                provider_attestation=None,
                selected_version_id=selected_version_id,
                one_shot_verified=True,
                trusted_dedicated_backtest=True,
            )
        except (OSError, ValueError, PaperBacktestAttestationError, json.JSONDecodeError):
            pass

    ordered = sorted(candidates.values(), key=lambda item: (item["created_run_id"], item["strategy_version_id"]), reverse=True)
    return {
        "items": ordered,
        "deployment_mode": "fixed_one_shot_paper_integration",
        "supported_order": {
            "instrument": "AAPL",
            "side": "BUY",
            "quantity": 1,
            "order_type": "LMT",
            "limit_price": "400.00",
            "time_in_force": "DAY",
        },
    }


def _candidate(*, version, run_id: str, report_sha256: str, report: dict[str, Any],
               provider_attestation: dict[str, Any] | None,
               selected_version_id: str | None, one_shot_verified: bool,
               trusted_dedicated_backtest: bool = False) -> dict[str, Any]:
    compatibility_reasons = paper_fixed_order_compatibility_reasons(
        version, report, provider_attestation=provider_attestation,
        trusted_dedicated_backtest=trusted_dedicated_backtest,
    )
    attested_current = version.version_id == selected_version_id and one_shot_verified
    return {
        "strategy_version_id": version.version_id,
        "strategy_id": version.strategy_id,
        "source_kind": version.source_kind.value,
        "instrument_ids": list(version.spec.instrument_ids),
        "execution_order_type": version.spec.execution_policy.order_type,
        "execution_time_in_force": version.spec.execution_policy.time_in_force,
        "position_mode": version.spec.position_policy.mode.value,
        "run_id": run_id,
        "created_run_id": run_id,
        "backtest_status": "verified",
        "backtest_report_sha256": report_sha256,
        "data_manifest_hash": report.get("data_manifest_hash"),
        "evaluation_start": (report.get("data_manifest") or {}).get("evaluation_start"),
        "evaluation_end": (report.get("data_manifest") or {}).get("evaluation_end"),
        "paper_deployable": not compatibility_reasons,
        "currently_deployed": attested_current,
        "paper_block_reasons": compatibility_reasons,
    }


def paper_fixed_order_compatibility_reasons(
    version, report: dict[str, Any], *,
    provider_attestation: dict[str, Any] | None = None,
    trusted_dedicated_backtest: bool = False,
) -> list[str]:
    """Prove a candidate's frozen policy and replay fit the authorized order.

    Compatibility is intentionally derived from both immutable strategy
    policy and the verified Backtrader output. A strategy cannot become
    deployable merely because its instrument or order type looks similar.
    """
    spec = version.spec
    reasons: list[str] = []
    if version.source_kind.value != "python_upload":
        reasons.append("source_kind_not_supported_by_fixed_python_upload_composition")
    if tuple(spec.instrument_ids) != ("AAPL",):
        reasons.append("instrument_outside_fixed_AAPL_test_scope")
    bar = spec.bar_policy
    if not (
        bar.timeframe == "1d"
        and bar.session == "regular"
        and bar.completed_only is True
        and bar.calendar_id == "us_equity_rth"
        and bar.timezone == "America/New_York"
    ):
        reasons.append("bar_policy_outside_completed_AAPL_RTH_daily_scope")
    execution = spec.execution_policy
    if not (
        execution.decision == "completed_bar_available"
        and execution.dispatch == "next_regular_session"
        and execution.order_type == "limit"
        and execution.time_in_force == "DAY"
        and execution.outside_rth is False
    ):
        reasons.append("execution_policy_outside_fixed_LMT_DAY_RTH_envelope")
    position = spec.position_policy
    if not (
        position.mode.value == "explicit_quantity"
        and position.long_only is True
        and position.whole_shares is True
        and spec.parameters.get("quantity") == 1
    ):
        reasons.append("position_sizing_outside_fixed_long_only_one_share_envelope")
    parameters = spec.parameters
    try:
        configured_limit = Decimal(str(parameters.get("limit_price_usd")))
    except (InvalidOperation, TypeError, ValueError):
        configured_limit = Decimal("NaN")
    if parameters.get("quantity") != 1 or parameters.get("order_type") != "limit" or not configured_limit.is_finite() or configured_limit != Decimal("400.00"):
        reasons.append("frozen_parameters_outside_fixed_LMT_400_order")

    manifest = report.get("data_manifest")
    if not trusted_dedicated_backtest and not _verified_paper_import_attestation(
        provider_attestation, report,
    ):
        reasons.append("backtest_data_lacks_server_verified_ibkr_paper_import")
    if (
        not isinstance(manifest, dict)
        or manifest.get("instrument_id") != "AAPL"
        or not manifest.get("evaluation_end")
        or manifest.get("evaluation_start") != manifest.get("evaluation_end")
    ):
        reasons.append("backtest_must_cover_one_aapl_evaluation_session")
    orders = report.get("orders")
    fills = report.get("fills")
    if not isinstance(orders, list) or len(orders) != 1:
        reasons.append("backtest_must_emit_exactly_one_order")
    else:
        order = orders[0]
        if not isinstance(order, dict) or not (
            order.get("side") == "BUY"
            and order.get("requested_qty") == 1
            and order.get("order_type") == "limit"
            and str(order.get("limit_price")) == "400.0"
            and order.get("decision_date") == manifest.get("evaluation_end")
            and order.get("filled_qty") == 0
            and order.get("status") == "OPEN_AT_DATA_END"
        ):
            reasons.append("backtest_order_differs_from_fixed_AAPL_BUY_1_LMT_400")
    if not isinstance(fills, list) or fills:
        reasons.append("backtest_must_have_no_fills_before_paper_deployment")
    return reasons


def _verified_paper_import_attestation(attestation: Any, report: dict[str, Any]) -> bool:
    """Match the API-issued import proof to the report's immutable data hashes."""
    if not isinstance(attestation, dict):
        return False
    manifest = report.get("data_manifest")
    if not isinstance(manifest, dict):
        return False
    download_parameters = manifest.get("download_parameters")
    if not isinstance(download_parameters, dict):
        return False
    embedded = download_parameters.get("application_ibkr_paper_import_attestation")
    quality = manifest.get("quality_report")
    return bool(
        attestation.get("schema_version") == "ibkr-paper-import-attestation/1"
        and attestation.get("profile_id") == "ibkr-paper-local"
        and attestation.get("environment") == "paper"
        and attestation.get("instrument_id") == manifest.get("instrument_id")
        and attestation.get("csv_sha256") == manifest.get("file_sha256")
        and attestation.get("calendar_schedule_sha256") == (
            quality.get("calendar_schedule_sha256") if isinstance(quality, dict) else None
        )
        and isinstance(attestation.get("con_id"), int)
        and not isinstance(attestation.get("con_id"), bool)
        and attestation.get("con_id") > 0
        and isinstance(attestation.get("retrieved_at"), str)
        and len(str(attestation.get("snapshot_sha256", ""))) == 64
        and embedded == attestation
        and str(manifest.get("provider", "")).startswith("IBKR PAPER Gateway")
    )


def _run_directories(root: Path) -> list[Path]:
    try:
        info = root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return []
        rows = []
        for path in root.iterdir():
            try:
                path_info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(path_info.st_mode) and stat.S_ISDIR(path_info.st_mode):
                rows.append((path_info.st_mtime_ns, path))
        return [path for _, path in sorted(rows, reverse=True)[:MAX_SCANNED_RUNS]]
    except OSError:
        return []


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
        raise ValueError("run artifact is not a bounded regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run artifact must contain a JSON object")
    return value


def _regular_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    except OSError:
        return False


def _regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    except OSError:
        return False


__all__ = [
    "MAX_SCANNED_RUNS",
    "list_paper_strategy_candidates",
    "paper_fixed_order_compatibility_reasons",
]
