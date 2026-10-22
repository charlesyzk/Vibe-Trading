"""Integrity sidecars for persisted Backtrader execution reports.

The sidecar detects accidental report loss or replacement. It is stored next
to the report on the same host and is not a signature or protection against a
local actor who can rewrite both files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "backtrader-execution-report-integrity/1"
REPORT_RELATIVE_PATH = "artifacts/execution_report.json"
SIDECAR_FILENAME = "execution_report.integrity.json"
EXPECTED_SCHEMA_CONFIG_KEY = "execution_report_integrity_schema"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def sidecar_path(report_path: str | Path) -> Path:
    return Path(report_path).with_name(SIDECAR_FILENAME)


def write_report_integrity_sidecar(report_path: str | Path) -> dict[str, Any]:
    """Atomically write the fixed-schema path, size, and SHA-256 binding."""
    path = Path(report_path)
    payload = path.read_bytes()
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_path": REPORT_RELATIVE_PATH,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    target = sidecar_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".execution_report_integrity.", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return record


def load_verified_report(report_path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read, validate, and parse one report byte snapshot.

    Callers that return report contents should use this value directly rather
    than reopening the path after verification, which would create a
    verification-to-read replacement window.
    """
    path = Path(report_path)
    try:
        report_bytes = path.read_bytes()
    except OSError:
        return None, "execution_report.json is missing or unreadable"
    try:
        record = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "execution report integrity sidecar is missing or invalid JSON"
    expected_keys = {"schema_version", "artifact_path", "size_bytes", "sha256"}
    if not isinstance(record, dict) or set(record) != expected_keys:
        return None, "execution report integrity sidecar has an invalid schema"
    if record["schema_version"] != SCHEMA_VERSION or record["artifact_path"] != REPORT_RELATIVE_PATH:
        return None, "execution report integrity sidecar has an unsupported schema or path"
    if type(record["size_bytes"]) is not int or record["size_bytes"] < 0:
        return None, "execution report integrity sidecar has an invalid size"
    digest = record["sha256"]
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        return None, "execution report integrity sidecar has an invalid SHA-256 digest"
    if record["size_bytes"] != len(report_bytes):
        return None, "execution report size does not match its integrity sidecar"
    if digest != hashlib.sha256(report_bytes).hexdigest():
        return None, "execution report SHA-256 does not match its integrity sidecar"
    try:
        parsed = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "execution report is not valid UTF-8 JSON"
    if not isinstance(parsed, dict):
        return None, "execution report JSON must be an object"
    return parsed, None


def verify_report_integrity(report_path: str | Path) -> tuple[bool, str | None]:
    """Validate exact sidecar shape and bind it to the current report bytes."""
    parsed, error = load_verified_report(report_path)
    return parsed is not None, error
