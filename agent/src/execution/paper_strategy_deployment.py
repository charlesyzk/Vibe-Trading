"""Bind a verified compatible frozen strategy to the fixed PAPER deployment.

This is configuration only. It never connects to IBKR or opens the dispatch
gate; the existing explicit one-shot action and exact risk envelope remain
the only order path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.bt_runtime.report_artifact_integrity import load_verified_report
from src.config.paths import get_runtime_root
from src.execution.paper_backtest_attestation import (
    verify_paper_backtest_attestation,
    write_paper_backtest_attestation,
)
from src.execution.paper_integration_test_config import (
    read_paper_integration_test_config,
    write_paper_integration_test_config,
)
from src.execution.paper_strategy_candidates import (
    _read_json,
    _regular_directory,
    _regular_file,
    list_paper_strategy_candidates,
)
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


_VERSION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ORDER_TABLES = ("order_intent", "order_reservation", "broker_order_binding", "order_outbox")


class PaperStrategyDeploymentError(ValueError):
    """A candidate cannot safely replace the fixed PAPER strategy binding."""


def prepare_paper_strategy_selection(
    version_id: str,
    *,
    operator_subject: str,
    run_id: str | None = None,
    runtime_root: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a compatible candidate selection and its local audit evidence.

    Callers must serialize this operation with manager startup, confirm the
    runtime is stopped, and confirm the one-shot execution ledger is empty.
    The returned receipt contains a config backup so the caller can roll back
    if rebuilding/installing the in-memory composition fails.
    """
    if not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id):
        raise PaperStrategyDeploymentError("strategy version must be a lowercase SHA-256 id")
    if not isinstance(operator_subject, str) or not operator_subject.strip():
        raise PaperStrategyDeploymentError("an attributable operator subject is required")
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id)):
        raise PaperStrategyDeploymentError("candidate run id is invalid")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise PaperStrategyDeploymentError("selection timestamp must include a timezone")
    instant = instant.astimezone(timezone.utc)

    root = Path(runtime_root).expanduser() if runtime_root is not None else get_runtime_root()
    config_path = root / "paper-integration-test.json"
    try:
        current = read_paper_integration_test_config(config_path)
    except Exception as exc:
        raise PaperStrategyDeploymentError("fixed PAPER integration config is unavailable") from exc
    candidate = next(
        (item for item in list_paper_strategy_candidates(runtime_root=root)["items"]
         if item["strategy_version_id"] == version_id and (run_id is None or item["run_id"] == run_id)),
        None,
    )
    if candidate is None:
        raise PaperStrategyDeploymentError("no verified Backtrader report exists for this frozen version")
    if candidate.get("paper_deployable") is not True:
        raise PaperStrategyDeploymentError("candidate report or strategy policy is outside the fixed PAPER envelope")

    version = StrategyVersionStore(root / "strategy-versions").get_version(version_id)
    report_path, report_sha256 = _resolve_report(
        root, version, candidate["run_id"], expected_sha256=candidate["backtest_report_sha256"]
    )
    if report_sha256 != candidate["backtest_report_sha256"]:
        raise PaperStrategyDeploymentError("candidate report changed during selection")
    backtest_dir = root / "paper-integration-test" / "backtests"
    _ensure_private_directory(backtest_dir)
    filename = f"{instant.strftime('%Y%m%dT%H%M%SZ')}-{version_id}.json"
    target = backtest_dir / filename
    if target.exists() or target.is_symlink():
        raise PaperStrategyDeploymentError("deployment report destination already exists")
    _atomic_copy_report(report_path, target)
    try:
        if hashlib.sha256(target.read_bytes()).hexdigest() != report_sha256:
            raise PaperStrategyDeploymentError("candidate report changed during selection")
        attestation = write_paper_backtest_attestation(target, version_id=version_id)
        # Read-after-write validates that the deployment will load the same
        # report from the location used by the app composition.
        verify_paper_backtest_attestation(backtest_dir, version=version, report_sha256=report_sha256)
        previous_bytes = config_path.read_bytes()
        updated_data = current.model_dump(mode="json")
        updated_data["strategy_version_id"] = version_id
        updated_data["backtest_report_sha256"] = report_sha256
        updated = type(current).model_validate(updated_data)
        write_paper_integration_test_config(updated, config_path)
    except Exception:
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".integrity.json").unlink(missing_ok=True)
        raise

    receipt = {
        "root": str(root),
        "config_path": str(config_path),
        "previous_config_bytes": previous_bytes,
        "report_path": str(target),
        "sidecar_path": str(target.with_name(target.name + ".integrity.json")),
        "previous_version_id": current.strategy_version_id,
        "strategy_version_id": version_id,
        "run_id": candidate["run_id"],
        "report_sha256": report_sha256,
        "deployment_attestation_sha256": attestation["sha256"],
        "selected_at": instant.isoformat().replace("+00:00", "Z"),
        "operator_subject": operator_subject.strip(),
    }
    return receipt


def commit_paper_strategy_selection(receipt: dict[str, Any]) -> None:
    """Write a durable, owner-only record after the new composition is installed."""
    root = Path(receipt["root"])
    directory = root / "paper-integration-test" / "deployments"
    _ensure_private_directory(directory)
    record = {
        key: value for key, value in receipt.items()
        if not key.startswith("_") and key not in {"root", "previous_config_bytes"}
    }
    record["event"] = "strategy_selected"
    path = directory / "selection-audit.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PaperStrategyDeploymentError("selection audit target must be a regular file")
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def rollback_paper_strategy_selection(receipt: dict[str, Any]) -> None:
    """Restore the prior version binding if app composition replacement fails."""
    config_path = Path(receipt["config_path"])
    _atomic_write(config_path, receipt["previous_config_bytes"])
    for key in ("report_path", "sidecar_path"):
        Path(receipt[key]).unlink(missing_ok=True)


def paper_execution_ledger_is_empty(runtime_root: str | Path | None = None) -> bool:
    """Require a pristine one-shot ledger before replacing its strategy binding."""
    root = Path(runtime_root).expanduser() if runtime_root is not None else get_runtime_root()
    path = root / "paper-integration-test" / "execution.sqlite"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in _ORDER_TABLES:
                if table in names and connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                    return False
    except sqlite3.Error:
        return False
    return True


def _resolve_report(root: Path, version: StrategyVersionRecord, run_id: str, *, expected_sha256: str) -> tuple[Path, str]:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise PaperStrategyDeploymentError("candidate run id is invalid")
    if run_id == "paper-integration-test":
        evidence = verify_paper_backtest_attestation(
            root / "paper-integration-test" / "backtests",
            version=version,
            report_sha256=expected_sha256,
        )
        path = evidence.report_path
    else:
        run_dir = root / "runs" / run_id
        if not _regular_directory(run_dir):
            raise PaperStrategyDeploymentError("candidate run directory is unavailable")
        state = _read_json(run_dir / "state.json")
        config = _read_json(run_dir / "config.json")
        if state.get("status") != "success" or config.get("execution_backend") != "backtrader":
            raise PaperStrategyDeploymentError("candidate run is not a successful Backtrader replay")
        path = run_dir / "artifacts" / "execution_report.json"
        integrity_path = path.with_name("execution_report.integrity.json")
        if not _regular_directory(path.parent) or not _regular_file(path) or not _regular_file(integrity_path):
            raise PaperStrategyDeploymentError("candidate report artifact is incomplete")
        report, error = load_verified_report(path)
        if report is None or error:
            raise PaperStrategyDeploymentError("candidate report integrity verification failed")
        if (
            report.get("strategy_version_hash") != version.version_id
            or report.get("strategy_id") != version.strategy_id
            or report.get("execution_backend") != "backtrader"
            or report.get("execution_mode") != "backtest"
            or report.get("instrument_id") not in version.spec.instrument_ids
            or report.get("source_kind") != version.source_kind.value
            or (report.get("execution_integrity") or {}).get("status") != "verified"
        ):
            raise PaperStrategyDeploymentError("candidate report provenance differs from its frozen version")
    raw = path.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    return path, hashlib.sha256(raw).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PaperStrategyDeploymentError("deployment artifact directory must not be a symlink")
    else:
        path.mkdir(parents=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_copy_report(source: Path, target: Path) -> None:
    raw = source.read_bytes()
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise PaperStrategyDeploymentError("candidate report has an invalid size")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".paper_candidate.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _atomic_write(path: Path, content: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".paper_config_rollback.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


__all__ = [
    "PaperStrategyDeploymentError",
    "commit_paper_strategy_selection",
    "paper_execution_ledger_is_empty",
    "prepare_paper_strategy_selection",
    "rollback_paper_strategy_selection",
]
