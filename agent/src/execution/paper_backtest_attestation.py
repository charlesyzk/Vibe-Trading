"""Bind a frozen PAPER strategy version to its verified Backtrader report."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.execution.contracts import is_sha256
from src.execution.versions import StrategyVersionRecord


SCHEMA_VERSION = "paper-backtest-attestation/1"
MAX_REPORT_BYTES = 16 * 1024 * 1024
_REPORT_NAME = re.compile(r"^\d{8}T\d{6}Z-([0-9a-f]{64})\.json$")


class PaperBacktestAttestationError(ValueError):
    """A frozen strategy has no matching intact successful Backtrader report."""


@dataclass(frozen=True, slots=True)
class PaperBacktestAttestation:
    version_id: str
    report_path: Path
    report_sha256: str
    data_manifest_hash: str
    evaluation_start: str | None
    evaluation_end: str | None


def _sidecar_path(report_path: Path) -> Path:
    return report_path.with_name(report_path.name + ".integrity.json")


def write_paper_backtest_attestation(report_path: str | Path, *, version_id: str) -> dict[str, Any]:
    """Write an atomic local digest binding after validating report identity."""
    path = Path(report_path)
    if not is_sha256(version_id):
        raise PaperBacktestAttestationError("strategy version id must be a SHA-256 hash")
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PaperBacktestAttestationError("backtest report must be a regular non-symlink file")
        report_bytes = path.read_bytes()
        if not 0 < len(report_bytes) <= MAX_REPORT_BYTES:
            raise PaperBacktestAttestationError("backtest report has an invalid size")
        report = json.loads(report_bytes.decode("utf-8"))
    except PaperBacktestAttestationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperBacktestAttestationError("backtest report is missing or invalid") from exc
    _validate_report(report, version_id=version_id)
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_filename": path.name,
        "size_bytes": len(report_bytes),
        "sha256": hashlib.sha256(report_bytes).hexdigest(),
        "strategy_version_id": version_id,
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    target = _sidecar_path(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".paper_backtest_attestation.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return record


def verify_paper_backtest_attestation(
    report_dir: str | Path,
    *,
    version: StrategyVersionRecord,
) -> PaperBacktestAttestation:
    """Load the newest exact-version report and verify its local digest and provenance."""
    if not isinstance(version, StrategyVersionRecord) or not is_sha256(version.version_id):
        raise PaperBacktestAttestationError("a verified frozen strategy version is required")
    directory = Path(report_dir).expanduser()
    try:
        directory_info = directory.lstat()
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise PaperBacktestAttestationError("backtest report directory must be a regular non-symlink directory")
        candidates = []
        for candidate in directory.iterdir():
            match = _REPORT_NAME.fullmatch(candidate.name)
            if match and match.group(1) == version.version_id:
                candidates.append(candidate)
    except PaperBacktestAttestationError:
        raise
    except OSError as exc:
        raise PaperBacktestAttestationError("backtest report directory is unavailable") from exc
    if not candidates:
        raise PaperBacktestAttestationError("no exact-version PAPER Backtrader report is available")

    for path in sorted(candidates, key=lambda item: item.name, reverse=True):
        try:
            report_info = path.lstat()
            sidecar = _sidecar_path(path)
            sidecar_info = sidecar.lstat()
            if (stat.S_ISLNK(report_info.st_mode) or not stat.S_ISREG(report_info.st_mode)
                    or stat.S_ISLNK(sidecar_info.st_mode) or not stat.S_ISREG(sidecar_info.st_mode)):
                continue
            report_bytes = path.read_bytes()
            if not 0 < len(report_bytes) <= MAX_REPORT_BYTES:
                continue
            integrity = json.loads(sidecar.read_text(encoding="utf-8"))
            report = json.loads(report_bytes.decode("utf-8"))
            digest = hashlib.sha256(report_bytes).hexdigest()
            if (not isinstance(integrity, dict)
                    or set(integrity) != {"schema_version", "artifact_filename", "size_bytes", "sha256", "strategy_version_id"}
                    or integrity.get("schema_version") != SCHEMA_VERSION
                    or integrity.get("artifact_filename") != path.name
                    or integrity.get("size_bytes") != len(report_bytes)
                    or integrity.get("sha256") != digest
                    or integrity.get("strategy_version_id") != version.version_id):
                continue
            _validate_report(report, version_id=version.version_id, version=version)
            manifest_hash = report["data_manifest_hash"]
            manifest = report["data_manifest"]
            evaluation_start = manifest.get("evaluation_start")
            evaluation_end = manifest.get("evaluation_end")
            return PaperBacktestAttestation(
                version_id=version.version_id,
                report_path=path,
                report_sha256=digest,
                data_manifest_hash=manifest_hash,
                evaluation_start=str(evaluation_start) if evaluation_start is not None else None,
                evaluation_end=str(evaluation_end) if evaluation_end is not None else None,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    raise PaperBacktestAttestationError("no intact, successful Backtrader report matches the frozen strategy")


def _validate_report(
    report: Any,
    *,
    version_id: str,
    version: StrategyVersionRecord | None = None,
) -> None:
    if not isinstance(report, dict):
        raise PaperBacktestAttestationError("backtest report must be a JSON object")
    integrity = report.get("execution_integrity")
    provenance = report.get("provenance")
    manifest_hash = report.get("data_manifest_hash")
    manifest = report.get("data_manifest")
    required = (
        report.get("strategy_version_hash") == version_id
        and isinstance(provenance, dict)
        and provenance.get("strategy_version_hash") == version_id
        and report.get("source_kind") == "python_upload"
        and report.get("engine", "").startswith("Backtrader ")
        and report.get("instrument_id") == "AAPL"
        and is_sha256(manifest_hash)
        and isinstance(manifest, dict)
        and isinstance(integrity, dict)
        and integrity.get("status") == "verified"
        and integrity.get("data_manifest_hash") == manifest_hash
        and type(report.get("evaluation_bar_count")) is int
        and report["evaluation_bar_count"] >= 1
    )
    if version is not None:
        expected_engine = f"Backtrader {version.engine_version.removeprefix('backtrader/')} / BackBroker"
        required = required and (
            report.get("strategy_id") == version.strategy_id
            and report.get("instrument_id") in version.spec.instrument_ids
            and report.get("engine") == expected_engine
        )
    if not required:
        raise PaperBacktestAttestationError("backtest report does not prove a successful exact-version Backtrader run")


__all__ = [
    "PaperBacktestAttestation",
    "PaperBacktestAttestationError",
    "verify_paper_backtest_attestation",
    "write_paper_backtest_attestation",
]
