"""Run listing and detail HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_runs_routes(app, ...)``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Generator, List, Literal, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helper functions (module-level; host Pydantic models resolved via sys.modules)
# ---------------------------------------------------------------------------

_FACTOR_MAX_RESULTS = 20
_FACTOR_MAX_SCAN_ENTRIES = 4096
_FACTOR_MAX_SCAN_DEPTH = 8
_FACTOR_MAX_FILE_BYTES = 2 * 1024 * 1024
_FACTOR_MAX_SUMMARY_BYTES = 128 * 1024
_FACTOR_MAX_TOTAL_ARTIFACT_BYTES = 8 * 1024 * 1024
_FACTOR_MAX_ROWS_PER_CSV = 5000
_FACTOR_MAX_TOTAL_ROWS = 20_000
_FACTOR_MAX_GROUP_COLUMNS = 100
_PREVIEW_AUTHORIZATION_MAX_SINGLE_BYTES = 80 * 1024 * 1024
_PREVIEW_AUTHORIZATION_MAX_ACTIVE_BYTES = 256 * 1024 * 1024
_BACKTRADER_RUN_DEFAULTS: Dict[str, Any] = {
    "strategy_budget": 40_000,
    "fee_reserve": 25,
    "price_buffer_fraction": 0.01,
    "commission_rate": 0.001,
    "slippage_fraction": 0.0,
    "initial_cash": 100_000,
    "interval": "1D",
    "engine": "daily",
    "execution_backend": "backtrader",
}


class BacktraderBacktestRequest(BaseModel):
    """Bounded request for the first user-created trusted-template run."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_id: str = Field(min_length=1, max_length=64)
    start_date: str
    end_date: str
    source: Literal["yfinance", "stooq", "ibkr"] = "yfinance"
    ibkr_profile_id: Optional[Literal["ibkr-paper-local", "ibkr-live-local-readonly"]] = None
    ibkr_port: Optional[Literal[4001, 4002, 7496, 7497]] = None
    strategy_description: str = Field(min_length=1, max_length=4096)
    allocation_fraction: float = Field(default=0.95, gt=0, le=1, allow_inf_nan=False)
    preview_token: Optional[str] = Field(default=None, min_length=32, max_length=128)
    clarification_transcript_id: Optional[str] = Field(default=None, max_length=36)
    clarification_turn_id: Optional[str] = Field(default=None, max_length=36)
    clarification_parent_turn_id: Optional[str] = Field(default=None, max_length=36)
    clarification_answer: Optional[str] = Field(default=None, max_length=512)

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,31}", value):
            raise ValueError("symbol contains unsupported characters")
        return value

    @field_validator("instrument_id")
    @classmethod
    def valid_instrument_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
            raise ValueError("instrument_id contains unsupported characters")
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_iso_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("dates must use YYYY-MM-DD") from None
        return value

    @field_validator("strategy_description")
    @classmethod
    def normalized_strategy_language(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def valid_request_window(self) -> "BacktraderBacktestRequest":
        start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(self.end_date, "%Y-%m-%d").date()
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        if (end - start).days > 3653:
            raise ValueError("date range is limited to ten years")
        if self.source == "ibkr" and self.ibkr_profile_id is None:
            raise ValueError("ibkr_profile_id is required for IBKR history")
        valid_ibkr_ports = {4002, 7497} if self.ibkr_profile_id == "ibkr-paper-local" else {4001, 7496}
        if self.ibkr_port is not None and (
            self.source != "ibkr" or self.ibkr_port not in valid_ibkr_ports
        ):
            raise ValueError("ibkr_port must match the selected IBKR read-only environment")
        if self.source != "ibkr" and self.ibkr_profile_id is not None:
            raise ValueError("ibkr_profile_id is only valid when source is ibkr")
        return self


class PythonUploadSessionRequest(BaseModel):
    """One caller-supplied regular session used to validate an imported bar."""

    model_config = ConfigDict(extra="forbid")

    session_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool = False


class IbkrPythonMarketDataRequest(BaseModel):
    """Request an exact, read-only IBKR daily snapshot for Python upload."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_id: str = Field(min_length=1, max_length=64)
    profile_id: Literal["ibkr-paper-local", "ibkr-live-local-readonly"]
    port: Optional[Literal[4001, 4002, 7496, 7497]] = None
    warmup_start: date
    warmup_end: date
    evaluation_start: date
    evaluation_end: date

    @field_validator("symbol")
    @classmethod
    def valid_ibkr_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._:-]{0,31}", value):
            raise ValueError("symbol contains unsupported characters")
        return value

    @field_validator("instrument_id")
    @classmethod
    def valid_ibkr_instrument(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
            raise ValueError("instrument_id contains unsupported characters")
        return value

    @model_validator(mode="after")
    def valid_ibkr_window(self) -> "IbkrPythonMarketDataRequest":
        if not (
            self.warmup_start <= self.warmup_end < self.evaluation_start <= self.evaluation_end
        ):
            raise ValueError("date ranges must satisfy warmup_start <= warmup_end < evaluation_start <= evaluation_end")
        if (self.evaluation_end - self.warmup_start).days > 3653:
            raise ValueError("date range is limited to ten years")
        valid_ports = {4002, 7497} if self.profile_id == "ibkr-paper-local" else {4001, 7496}
        if self.port is not None and self.port not in valid_ports:
            raise ValueError("socket port does not match the selected IBKR environment")
        return self


class PythonUploadBacktestRequest(BaseModel):
    """Bounded Python-upload offline backtest with explicit bar evidence."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    strategy_source: str = Field(min_length=1, max_length=262_144)
    market_data_csv: str = Field(min_length=1, max_length=64 * 1024 * 1024)
    market_data_metadata: Dict[str, Any]
    expected_sessions: List[PythonUploadSessionRequest] = Field(min_length=1, max_length=100_000)
    initial_cash: float = Field(default=100_000, gt=0, allow_inf_nan=False)
    commission_rate: float = Field(default=0.001, ge=0, le=1, allow_inf_nan=False)
    slippage_fraction: float = Field(default=0.0, ge=0, le=0.25, allow_inf_nan=False)
    preview_token: Optional[str] = Field(default=None, min_length=32, max_length=128)

    @field_validator("strategy_id", "instrument_id")
    @classmethod
    def valid_python_identity(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
            raise ValueError("identity contains unsupported characters")
        return value

    @field_validator("strategy_source")
    @classmethod
    def require_python_callback(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("strategy_source must not be blank")
        if len(value.encode("utf-8")) > 256 * 1024:
            raise ValueError("strategy source exceeds 256 KiB")
        return value

    @field_validator("market_data_csv")
    @classmethod
    def require_bounded_csv_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 64 * 1024 * 1024:
            raise ValueError("market-data CSV exceeds 64 MiB")
        return value


class PythonUploadBacktestConfirmationRequest(BaseModel):
    """Confirmation identity; market data is loaded from the preview token."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    strategy_source: str = Field(min_length=1, max_length=262_144)
    initial_cash: float = Field(default=100_000, gt=0, allow_inf_nan=False)
    commission_rate: float = Field(default=0.001, ge=0, le=1, allow_inf_nan=False)
    slippage_fraction: float = Field(default=0.0, ge=0, le=0.25, allow_inf_nan=False)
    preview_token: Optional[str] = Field(default=None, min_length=32, max_length=128)
    # Accepted for compatibility with clients that still resend the preview
    # body. These fields are deliberately ignored; execution loads the stored
    # preview snapshot and never trusts confirmation-time market data.
    market_data_csv: Optional[str] = Field(default=None, min_length=1, max_length=64 * 1024 * 1024)
    market_data_metadata: Optional[Dict[str, Any]] = None
    expected_sessions: Optional[List[PythonUploadSessionRequest]] = Field(default=None, max_length=100_000)

    @field_validator("strategy_id", "instrument_id")
    @classmethod
    def valid_python_identity(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value):
            raise ValueError("identity contains unsupported characters")
        return value

    @field_validator("strategy_source")
    @classmethod
    def require_bounded_strategy_source(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value.encode("utf-8")) > 256 * 1024:
            raise ValueError("strategy source must be non-empty and at most 256 KiB")
        return value

    @field_validator("market_data_csv")
    @classmethod
    def require_bounded_optional_csv(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.encode("utf-8")) > 64 * 1024 * 1024:
            raise ValueError("market-data CSV exceeds 64 MiB")
        return value


def _parse_trusted_sma_description(value: str) -> Tuple[int, int]:
    """Parse only the complete, explicitly supported SMA-cross sentences.

    This small grammar avoids pretending to understand filters, stops, or
    other conditions that the current compiler cannot execute. A later NL2
    model may produce this same frozen representation after clarification.
    """
    text = " ".join(value.strip().split())
    chinese = re.fullmatch(
        r"(?:当)?(?P<fast>\d+)(?:日|天)?(?:SMA|均线)(?:上穿|金叉)"
        r"(?P<slow>\d+)(?:日|天)?(?:SMA|均线)(?:买入|开仓|做多)?"
        r"[，,；;。 ]+(?:当)?(?P=fast)(?:日|天)?(?:SMA|均线)(?:下穿|死叉)"
        r"(?P=slow)(?:日|天)?(?:SMA|均线)(?:卖出|平仓|清仓)?",
        text,
        flags=re.IGNORECASE,
    )
    english = re.fullmatch(
        r"(?:buy|enter) when (?P<fast>\d+)[ -]day (?:SMA|moving average) crosses above "
        r"(?P<slow>\d+)[ -]day (?:SMA|moving average); (?:sell|exit) when "
        r"(?P=fast)[ -]day (?:SMA|moving average) crosses below "
        r"(?P=slow)[ -]day (?:SMA|moving average)\.?",
        text,
        flags=re.IGNORECASE,
    )
    match = chinese or english
    if match is None:
        raise ValueError(
            "unsupported strategy description; use the complete supported SMA cross rule "
            "(fast average crosses above slow to enter, and below to exit). Extra conditions are not supported"
        )
    fast, slow = int(match.group("fast")), int(match.group("slow"))
    if not (1 <= fast < slow <= 250):
        raise ValueError("SMA periods must satisfy 1 <= fast < slow <= 250")
    return fast, slow


def _public_python_strategy_failure(exc: Exception) -> dict[str, Any] | None:
    """Build a safe, bounded API failure from trusted normalized runtime fields."""
    from src.bt_runtime.strategies.python_upload import PythonStrategyRunError

    if not isinstance(exc, PythonStrategyRunError):
        return None
    failure = exc.public_failure if isinstance(exc.public_failure, dict) else {}
    category = failure.get("category")
    if category not in {"strategy_runtime_error", "strategy_contract_error"}:
        category = "strategy_execution_error"
    error_type = failure.get("error_type")
    allowed_types = {
        "AssertionError", "AttributeError", "IndexError", "KeyError", "NameError",
        "OverflowError", "TypeError", "ValueError", "ZeroDivisionError", "OtherError",
    }
    if error_type not in allowed_types:
        error_type = None
    seq = exc.event_seq if type(exc.event_seq) is int and 1 <= exc.event_seq <= 100_000 else None
    source_line = failure.get("source_line")
    if type(source_line) is not int or not 1 <= source_line <= 262_144:
        source_line = None
    if category == "strategy_runtime_error":
        message = "Strategy code raised an error"
        if error_type:
            message += f" ({error_type})"
        if source_line:
            message += f" on source line {source_line}"
    elif category == "strategy_contract_error":
        message = "Strategy output did not match the required on_event contract"
    else:
        message = "Strategy execution failed validation"
    if seq is not None:
        message = f"Bar {seq}: {message}"
    return {
        "status": "failed",
        "error_code": category,
        "event_seq": seq,
        "error_type": error_type,
        "source_line": source_line,
        "message": message,
    }


def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON from disk if present."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _load_csv_to_dict(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load CSV rows into a list of dictionaries."""
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        if limit is not None:
            rows = rows[:limit]
        return rows
    except Exception:
        return []


def _finite_float(value: Any) -> Optional[float]:
    """Parse a finite float, returning ``None`` for invalid values.

    Args:
        value: Candidate numeric value.

    Returns:
        Parsed finite float, or ``None``.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sanitize_finite_json(value: Any) -> Any:
    """Replace non-finite JSON numeric leaves with ``None`` recursively.

    Args:
        value: Parsed JSON value.

    Returns:
        JSON-compatible value without NaN or infinities.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_finite_json(item) for item in value]
    return value


def _claim_factor_artifact(
    path: Path,
    *,
    max_file_bytes: int,
    remaining_bytes: int,
) -> Tuple[bool, int, bool]:
    """Check whether an artifact can be read inside the response byte budget.

    Symlinks are rejected so companion files cannot escape the discovered
    bundle. The returned tuple is ``(readable, charged_bytes, degraded)``;
    missing optional files are not considered degraded.

    Args:
        path: Candidate artifact path.
        max_file_bytes: Per-file byte ceiling.
        remaining_bytes: Remaining aggregate byte budget.

    Returns:
        Tuple of readability, charged bytes, and degraded status.
    """
    try:
        if path.is_symlink():
            return False, 0, True
        if not path.is_file():
            return False, 0, False
        size = path.stat().st_size
    except OSError:
        return False, 0, True
    if size < 0 or size > max_file_bytes or size > remaining_bytes:
        return False, 0, True
    return True, size, False


def _load_ic_series_csv(path: Path, row_limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Load an ``ic_series.csv`` file into ``{"date", "ic"}`` rows.

    The first CSV column is treated as the date column regardless of its
    header name (the factor tool writes it with an empty or varying index
    label). Rows whose IC value does not parse as a float are skipped.

    Args:
        path: Path to the ic_series.csv file.
        row_limit: Maximum source rows to parse.

    Returns:
        Tuple of rows with raw string dates and finite IC values plus a flag
        indicating that the source was truncated or could not be parsed.
    """
    if row_limit <= 0:
        return [], True
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames
            if not fieldnames or fieldnames[0] is None:
                return [], True
            date_col = fieldnames[0]
            rows: List[Dict[str, Any]] = []
            truncated = False
            for source_index, row in enumerate(reader):
                if source_index >= row_limit:
                    truncated = True
                    break
                date_value = row.get(date_col)
                if date_value is None:
                    continue
                ic_value = _finite_float(row.get("IC"))
                if ic_value is None:
                    continue
                rows.append({"date": date_value, "ic": ic_value})
            return rows, truncated
    except Exception:
        return [], True


def _load_group_equity_csv(
    path: Path,
    row_limit: int,
) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    """Load a ``group_equity.csv`` file into dated rows of float group values.

    The first column is the date; every remaining column is kept in file
    order and parsed as float. Cells that fail to parse are dropped.

    Args:
        path: Path to the group_equity.csv file.
        row_limit: Maximum source rows to parse.

    Returns:
        Tuple of (rows, value column names in file order, truncated/degraded).
    """
    if row_limit <= 0:
        return [], [], True
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames
            if not fieldnames or fieldnames[0] is None or len(fieldnames) < 2:
                return [], [], True
            date_col = fieldnames[0]
            all_value_cols = [col for col in fieldnames[1:] if col]
            value_cols = all_value_cols[:_FACTOR_MAX_GROUP_COLUMNS]
            truncated = len(value_cols) < len(all_value_cols)
            rows: List[Dict[str, Any]] = []
            for source_index, row in enumerate(reader):
                if source_index >= row_limit:
                    truncated = True
                    break
                date_value = row.get(date_col)
                if date_value is None:
                    continue
                parsed: Dict[str, Any] = {"date": date_value}
                for col in value_cols:
                    numeric_value = _finite_float(row.get(col))
                    if numeric_value is not None:
                        parsed[col] = numeric_value
                rows.append(parsed)
            return rows, value_cols, truncated
    except Exception:
        return [], [], True


def _load_factor_summary(path: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Load a factor summary while removing non-finite numeric leaves.

    Args:
        path: Path to ``ic_summary.json``.

    Returns:
        Sanitized summary and whether parsing degraded.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None, True
        return _sanitize_finite_json(payload), False
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, True


def _iter_factor_series_files(
    run_dir: Path,
    *,
    max_results: int,
) -> Generator[Path, None, None]:
    """Yield bounded, non-symlinked ``ic_series.csv`` paths.

    Directory entries, nesting depth, and result count are all capped. Unlike
    ``Path.rglob()``, this iterator never constructs the complete match list
    and never follows a symlinked directory.

    Args:
        run_dir: Root directory of the run.
        max_results: Maximum factor marker paths to yield.

    Yields:
        Safe paths to factor IC-series artifacts.
    """
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir() or artifacts_dir.is_symlink() or max_results <= 0:
        return

    pending: Deque[Tuple[Path, int]] = deque([(artifacts_dir, 0)])
    entries_seen = 0
    yielded = 0
    while pending and entries_seen < _FACTOR_MAX_SCAN_ENTRIES and yielded < max_results:
        directory, depth = pending.popleft()
        child_directories: List[Path] = []
        try:
            with os.scandir(directory) as scan:
                iterator = iter(scan)
                while entries_seen < _FACTOR_MAX_SCAN_ENTRIES:
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    entries_seen += 1
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.name == "ic_series.csv" and entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                            yielded += 1
                            if yielded >= max_results:
                                return
                        elif depth < _FACTOR_MAX_SCAN_DEPTH and entry.is_dir(follow_symlinks=False):
                            child_directories.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
        child_directories.sort(key=lambda path: path.name)
        pending.extend((path, depth + 1) for path in child_directories)


def _has_factor_artifacts(run_dir: Path) -> bool:
    """Return whether a bounded directory-entry scan finds a factor bundle."""
    candidates = _iter_factor_series_files(run_dir, max_results=1)
    try:
        return next(candidates, None) is not None
    finally:
        candidates.close()


def _scan_factor_results(run_dir: Path) -> List[Dict[str, Any]]:
    """Scan a run directory for factor-analysis artifact bundles.

    A factor result is any directory under ``run_dir/artifacts`` (recursive)
    that contains an ``ic_series.csv`` file. Traversal, nesting, factor count,
    file sizes, source rows, columns, and aggregate response inputs are all
    bounded. Symlinked directories and files are ignored.

    Args:
        run_dir: Root directory of the run.
    Returns:
        Factor result dicts sorted by name ascending.
    """
    artifacts_dir = run_dir / "artifacts"
    results: List[Dict[str, Any]] = []
    remaining_bytes = _FACTOR_MAX_TOTAL_ARTIFACT_BYTES
    remaining_rows = _FACTOR_MAX_TOTAL_ROWS
    for csv_path in _iter_factor_series_files(run_dir, max_results=_FACTOR_MAX_RESULTS):
        try:
            relative_dir = csv_path.parent.relative_to(run_dir)
        except ValueError:
            continue
        directory = csv_path.parent
        name = "factor_analysis" if directory == artifacts_dir else directory.name

        truncated: Dict[str, bool] = {}
        ic_rows: List[Dict[str, Any]] = []
        ic_readable, ic_size, ic_degraded = _claim_factor_artifact(
            csv_path,
            max_file_bytes=_FACTOR_MAX_FILE_BYTES,
            remaining_bytes=remaining_bytes,
        )
        if ic_readable:
            remaining_bytes -= ic_size
            ic_rows, ic_rows_truncated = _load_ic_series_csv(
                csv_path,
                min(_FACTOR_MAX_ROWS_PER_CSV, remaining_rows),
            )
            remaining_rows -= len(ic_rows)
            if ic_rows_truncated:
                truncated["ic_series"] = True
        elif ic_degraded:
            truncated["ic_series"] = True

        summary: Optional[Dict[str, Any]] = None
        summary_path = directory / "ic_summary.json"
        summary_readable, summary_size, summary_degraded = _claim_factor_artifact(
            summary_path,
            max_file_bytes=_FACTOR_MAX_SUMMARY_BYTES,
            remaining_bytes=remaining_bytes,
        )
        if summary_readable:
            remaining_bytes -= summary_size
            summary, summary_parse_degraded = _load_factor_summary(summary_path)
            if summary_parse_degraded:
                truncated["ic_stats"] = True
        elif summary_degraded:
            truncated["ic_stats"] = True

        equity_rows: List[Dict[str, Any]] = []
        equity_cols: List[str] = []
        equity_path = directory / "group_equity.csv"
        equity_readable, equity_size, equity_degraded = _claim_factor_artifact(
            equity_path,
            max_file_bytes=_FACTOR_MAX_FILE_BYTES,
            remaining_bytes=remaining_bytes,
        )
        if equity_readable:
            remaining_bytes -= equity_size
            equity_rows, equity_cols, equity_rows_truncated = _load_group_equity_csv(
                equity_path,
                min(_FACTOR_MAX_ROWS_PER_CSV, remaining_rows),
            )
            remaining_rows -= len(equity_rows)
            if equity_rows_truncated:
                truncated["group_equity"] = True
        elif equity_degraded:
            truncated["group_equity"] = True

        group_cols = [col for col in equity_cols if col.startswith("Group_")]
        spread_cols = group_cols or equity_cols
        long_short_spread: Optional[float] = None
        group_final_equity: Dict[str, Any] = {}
        if equity_rows and spread_cols:
            last_row = equity_rows[-1]
            first_value = last_row.get(spread_cols[0])
            last_value = last_row.get(spread_cols[-1])
            if first_value is not None and last_value is not None:
                spread = last_value - first_value
                if math.isfinite(spread):
                    long_short_spread = round(spread, 6)
            group_final_equity = {col: last_row[col] for col in equity_cols if col in last_row}
        entry: Dict[str, Any] = {
            "name": name,
            "path": relative_dir.as_posix(),
            "ic_series": ic_rows,
        }
        if isinstance(summary, dict):
            entry["ic_stats"] = summary
        entry["group_equity"] = equity_rows
        entry["n_groups"] = len(group_cols)
        entry["long_short_spread"] = long_short_spread
        entry["group_final_equity"] = group_final_equity
        if truncated:
            entry["truncated"] = truncated
        results.append(entry)
    results.sort(key=lambda item: (str(item["name"]), str(item["path"])))
    return results


def _pearson_correlation(x_values: List[float], y_values: List[float]) -> float:
    """Compute Pearson correlation, degrading to 0.0 for degenerate input.

    Args:
        x_values: First series of values.
        y_values: Second series of values (same length).

    Returns:
        Pearson correlation in [-1, 1], or 0.0 when a variance is zero.
    """
    try:
        correlation = statistics.correlation(x_values, y_values)
    except (statistics.StatisticsError, ValueError, OverflowError, ZeroDivisionError):
        return 0.0
    if not math.isfinite(correlation):
        return 0.0
    return max(-1.0, min(1.0, correlation))


def _factor_ic_correlation(factors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build the pairwise IC correlation matrix across factor results.

    Args:
        factors: Factor result dicts from ``_scan_factor_results``.

    Returns:
        Labels plus a symmetric Pearson correlation matrix rounded to four
        decimals, or ``None`` when fewer than two factors exist or any pair
        shares fewer than 10 dates.
    """
    if len(factors) < 2:
        return None
    series: List[Dict[str, float]] = []
    for factor in factors:
        series.append({str(row["date"]): float(row["ic"]) for row in factor.get("ic_series") or []})
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            if len(series[i].keys() & series[j].keys()) < 10:
                return None
    size = len(series)
    matrix: List[List[float]] = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            common_dates = sorted(series[i].keys() & series[j].keys())
            x_values = [series[i][date] for date in common_dates]
            y_values = [series[j][date] for date in common_dates]
            correlation = round(_pearson_correlation(x_values, y_values), 4)
            matrix[i][j] = correlation
            matrix[j][i] = correlation
    return {"labels": [str(factor["name"]) for factor in factors], "matrix": matrix}


# Bounded contract for /runs/{run_id}/positions/sectors: at most
# _POSITIONS_SECTOR_MAX_SYMBOLS symbols get a network industry lookup (excess
# symbols degrade to asset-class-only grouping), lookups run on ONE shared
# module-level executor with _POSITIONS_SECTOR_WORKERS threads (created lazily
# on first use and never shut down — process-lifetime — so concurrent requests
# share a single bounded pool instead of spawning one per request), and each
# HTTP call carries the shared Eastmoney client's 15-second socket timeout.
_POSITIONS_SECTOR_MAX_SYMBOLS = 200
_POSITIONS_SECTOR_WORKERS = 4
_POSITIONS_SECTOR_EXECUTOR: Optional[ThreadPoolExecutor] = None
_POSITIONS_SECTOR_EXECUTOR_LOCK = threading.Lock()


def _get_positions_sector_executor() -> ThreadPoolExecutor:
    """Return the shared, process-lifetime industry-lookup executor.

    Created lazily on first use with ``_POSITIONS_SECTOR_WORKERS`` workers and
    never shut down, so every request fans out onto the same bounded pool.

    Returns:
        The shared executor instance.
    """
    global _POSITIONS_SECTOR_EXECUTOR
    if _POSITIONS_SECTOR_EXECUTOR is None:
        with _POSITIONS_SECTOR_EXECUTOR_LOCK:
            if _POSITIONS_SECTOR_EXECUTOR is None:
                _POSITIONS_SECTOR_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_POSITIONS_SECTOR_WORKERS,
                    thread_name_prefix="positions-sector",
                )
    return _POSITIONS_SECTOR_EXECUTOR


def _read_positions_symbols(path: Path) -> List[str]:
    """Read the symbol column names from a wide-format ``positions.csv``.

    Args:
        path: Path to ``artifacts/positions.csv`` (header
            ``timestamp,<sym1>,<sym2>,...``).

    Returns:
        Symbol column names in file order; empty when the file is missing,
        a symlink, empty, or unreadable.
    """
    try:
        if not path.exists() or path.is_symlink():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            fieldnames = csv.DictReader(handle).fieldnames
    except Exception:
        return []
    if not fieldnames:
        return []
    return [name.strip() for name in fieldnames[1:] if name and name.strip()]


def _resolve_industries_concurrent(symbols: List[str]) -> Dict[str, Optional[str]]:
    """Resolve A-share industry boards with bounded concurrency.

    Args:
        symbols: A-share symbols to resolve (already capped by the caller).

    Returns:
        Mapping of symbol -> industry board name, or ``None`` when the lookup
        failed or found no board row. Never raises.
    """
    from src.tools.sector_tool import resolve_industry_board

    results: Dict[str, Optional[str]] = {}
    pool = _get_positions_sector_executor()
    futures = {pool.submit(resolve_industry_board, symbol): symbol for symbol in symbols}
    for future in as_completed(futures):
        symbol = futures[future]
        try:
            results[symbol] = future.result()
        except Exception:  # noqa: BLE001 - one failure must not abort the batch
            results[symbol] = None
    return results


def _build_positions_sector_map(run_dir: Path, run_id: str, *, refresh: bool) -> Dict[str, Any]:
    """Resolve asset class + A-share industry for a run's position symbols.

    Serves from the ``artifacts/sector_map.json`` cache when a valid cache
    exists and ``refresh`` is false; otherwise recomputes and rewrites the
    cache. A corrupt cache file is treated as a cache miss.

    Blocking work here (file reads plus throttled Eastmoney HTTP) is bounded
    by the module-level contract: at most ``_POSITIONS_SECTOR_MAX_SYMBOLS``
    network lookups, ``_POSITIONS_SECTOR_WORKERS`` concurrent workers, and the
    shared Eastmoney client's 15-second per-request socket timeout. Symbols
    beyond the cap keep their asset class but skip industry resolution. The
    caller must run this off the event loop (see ``run_in_threadpool``).

    Args:
        run_dir: Persisted run directory containing ``artifacts/``.
        run_id: Run identifier echoed back in the response envelope.
        refresh: True bypasses the disk cache.

    Returns:
        The envelope ``{"ok", "run_id", "resolved_at", "cached", "symbols",
        "unresolved", "total_symbols", "symbol_limit"}``, or the ``{"ok",
        "run_id", "symbols", "note"}`` form when no positions artifact exists.
    """
    from backtest.engines._market_hooks import _detect_market

    artifacts_dir = run_dir / "artifacts"
    cache_path = artifacts_dir / "sector_map.json"
    try:
        symlinked = artifacts_dir.is_symlink() or cache_path.is_symlink()
    except OSError:
        symlinked = False
    if symlinked:
        # Mirror the factor scan's symlink rejection: a symlinked artifacts dir
        # — or a symlinked sector_map.json cache file inside a real one — is
        # treated as having no positions artifact rather than followed, so
        # neither the cache read nor the cache rewrite can escape the run dir.
        return {"ok": True, "run_id": run_id, "symbols": {}, "note": "no positions artifact"}

    if not refresh:
        cached = _load_json_file(cache_path)
        if isinstance(cached, dict) and isinstance(cached.get("symbols"), dict):
            cached["cached"] = True
            cached["run_id"] = run_id
            return cached

    symbols = _read_positions_symbols(run_dir / "artifacts" / "positions.csv")
    if not symbols:
        return {"ok": True, "run_id": run_id, "symbols": {}, "note": "no positions artifact"}

    resolved: Dict[str, Any] = {}
    a_share_to_resolve: List[str] = []
    for symbol in symbols:
        asset_class = _detect_market(symbol)
        resolved[symbol] = {
            "asset_class": asset_class,
            "industry": None,
            "industry_source": None,
        }
        # The budget counts A-share network lookups only — non-A-share symbols
        # never consume it, so a mixed book with a long non-A-share prefix
        # still resolves its first _POSITIONS_SECTOR_MAX_SYMBOLS A-shares.
        if asset_class == "a_share" and len(a_share_to_resolve) < _POSITIONS_SECTOR_MAX_SYMBOLS:
            a_share_to_resolve.append(symbol)

    if a_share_to_resolve:
        for symbol, industry in _resolve_industries_concurrent(a_share_to_resolve).items():
            if industry is not None:
                resolved[symbol]["industry"] = industry
                resolved[symbol]["industry_source"] = "eastmoney"

    unresolved = [
        symbol
        for symbol in symbols
        if resolved[symbol]["asset_class"] == "a_share" and resolved[symbol]["industry"] is None
    ]

    payload: Dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cached": False,
        "symbols": resolved,
        "unresolved": unresolved,
        "total_symbols": len(symbols),
        "symbol_limit": _POSITIONS_SECTOR_MAX_SYMBOLS,
    }
    tmp_path = cache_path.with_name(f".sector_map.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, cache_path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return payload


def _run_response_payload(response: Any) -> Dict[str, Any]:
    """Return a JSON-ready payload for opt-in run response variants."""
    return response.model_dump(mode="json")


def _build_response_from_run_dir(
    run_dir: Path,
    elapsed: float,
    *,
    include_analysis: bool = False,
    chart_symbol: Optional[str] = None,
    chart_payload: str = "full",
    chart_symbols_out: Optional[List[str]] = None,
):
    """Build a run response from a persisted run directory."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    RunResponse = host.RunResponse
    BacktestMetrics = host.BacktestMetrics
    RAGSelection = host.RAGSelection
    Artifact = host.Artifact

    run_id = run_dir.name

    response = RunResponse(
        status="unknown",
        run_id=run_id,
        elapsed_seconds=elapsed,
        run_directory=str(run_dir),
    )

    state_data = _load_json_file(run_dir / "state.json")
    if state_data:
        state_status = str(state_data.get("status") or "").lower()
        if state_status == "success":
            response.status = "success"
        elif state_status in {"failed", "cancelled"}:
            response.status = state_status
            response.reason = state_data.get("reason", "")
        else:
            response.status = state_status or "unknown"
    else:
        response.status = "unknown"

    request_data = _load_json_file(run_dir / "req.json") or {}
    response.prompt = request_data.get("prompt") or request_data.get("request")

    planner_path = run_dir / "planner_output.json"
    response.planner_output = _load_json_file(planner_path)

    design_path = run_dir / "design_spec.json"
    response.strategy_spec = _load_json_file(design_path)

    config_data = _load_json_file(run_dir / "config.json") or {}
    if response.strategy_spec is None and isinstance(config_data.get("strategy_spec"), dict):
        response.strategy_spec = config_data["strategy_spec"]
    response.execution_backend = config_data.get("execution_backend")

    execution_report_path = run_dir / "artifacts" / "execution_report.json"
    from src.bt_runtime.report_artifact_integrity import (
        EXPECTED_SCHEMA_CONFIG_KEY,
        SCHEMA_VERSION as REPORT_INTEGRITY_SCHEMA,
        load_verified_report,
        sidecar_path,
    )

    integrity_marker_present = EXPECTED_SCHEMA_CONFIG_KEY in config_data
    report_expected = config_data.get(EXPECTED_SCHEMA_CONFIG_KEY) == REPORT_INTEGRITY_SCHEMA
    report_exists = execution_report_path.is_file()
    report_sidecar_exists = sidecar_path(execution_report_path).is_file()
    integrity_status: str
    integrity_error: str | None = None
    execution_report = None
    if integrity_marker_present or report_sidecar_exists or report_exists:
        if integrity_marker_present and not report_expected:
            integrity_status = "invalid"
            integrity_error = "run config declares an unsupported execution report integrity schema"
        elif integrity_marker_present or report_sidecar_exists:
            execution_report, integrity_error = load_verified_report(execution_report_path)
            integrity_status = "verified" if execution_report is not None else "invalid"
        elif report_exists:
            # Pre-sidecar runs remain readable, but are never presented as verified.
            execution_report = _load_json_file(execution_report_path)
            if not isinstance(execution_report, dict):
                execution_report = None
                integrity_status = "invalid"
                integrity_error = "legacy execution report is missing, unreadable, or invalid JSON"
            else:
                integrity_status = "unverified_legacy"
        else:
            integrity_status = "invalid"
            integrity_error = "expected execution report is missing"
        response.execution_report_integrity = {
            "status": integrity_status,
            **({"error": integrity_error} if integrity_error else {}),
        }
    if isinstance(execution_report, dict):
        response.execution_report = execution_report
        response.execution_backend = execution_report.get("execution_backend") or response.execution_backend
        response.execution_mode = execution_report.get("execution_mode")
        response.strategy_version_id = execution_report.get("strategy_version_id")
        response.strategy_version_hash = execution_report.get("strategy_version_hash")

    rag_path = run_dir / "rag_metadata.json"
    rag_data = _load_json_file(rag_path)
    if rag_data:
        response.rag_selection = RAGSelection(
            selected_api=rag_data.get("selected_api") or rag_data.get("api_code", ""),
            selected_name=rag_data.get("selected_name") or rag_data.get("api_name", ""),
            selected_score=float(rag_data.get("selected_score") or rag_data.get("score", 0.0)),
        )

    metrics_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_path.exists():
        metrics_dict_list = _load_csv_to_dict(metrics_path, limit=1)
        if metrics_dict_list:
            row = metrics_dict_list[0]
            try:
                # Pass ALL CSV columns to BacktestMetrics (extra="allow")
                parsed: dict = {}
                for k, v in row.items():
                    if not k or not v:
                        continue
                    try:
                        parsed[k] = int(float(v)) if k == "trade_count" or k == "max_consecutive_loss" else float(v)
                    except (ValueError, TypeError):
                        continue
                if "final_value" in parsed:
                    response.metrics = BacktestMetrics(**parsed)
            except (ValueError, TypeError):
                pass

    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists():
        for file_path in artifacts_dir.iterdir():
            if file_path.is_file():
                file_type = file_path.suffix.lstrip(".")
                response.artifacts.append(
                    Artifact(
                        name=file_path.name,
                        path=str(file_path),
                        type=file_type if file_type else "unknown",
                        size=file_path.stat().st_size,
                        exists=True,
                    )
                )

    response.has_factor_artifacts = _has_factor_artifacts(run_dir)

    equity_path = run_dir / "artifacts" / "equity.csv"
    if equity_path.exists():
        response.artifacts_equity_csv = _load_csv_to_dict(equity_path)

    metrics_csv_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_csv_path.exists():
        response.artifacts_metrics_csv = _load_csv_to_dict(metrics_csv_path)

    run_card_path = run_dir / "run_card.json"
    if run_card_path.exists():
        try:
            response.run_card = json.loads(run_card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    risk_xray = _load_json_file(run_dir / "artifacts" / "risk_xray.json")
    if risk_xray is not None:
        response.risk_xray = risk_xray
    rebalance_notes = _load_json_file(run_dir / "artifacts" / "rebalance_notes.json")
    if rebalance_notes is not None:
        response.rebalance_notes = rebalance_notes

    llm_usage_path = run_dir / "llm_usage.json"
    if llm_usage_path.exists():
        try:
            response.llm_usage = json.loads(llm_usage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    trades_path = run_dir / "artifacts" / "trades.csv"
    if trades_path.exists():
        response.artifacts_trades_csv = _load_csv_to_dict(trades_path)

    positions_path = run_dir / "artifacts" / "positions.csv"
    if positions_path.exists():
        response.artifacts_positions_csv = _load_csv_to_dict(positions_path)

    target_positions_path = run_dir / "artifacts" / "target_positions.csv"
    if target_positions_path.exists():
        response.artifacts_target_positions_csv = _load_csv_to_dict(target_positions_path)

    validation_path = run_dir / "artifacts" / "validation.json"
    if validation_path.exists():
        try:
            response.validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if response.artifacts_equity_csv:
        filtered_equity = []
        for row in response.artifacts_equity_csv[:1000]:
            filtered_row: Dict[str, Any] = {}
            if "timestamp" in row:
                filtered_row["time"] = row["timestamp"]
            if "equity" in row:
                filtered_row["equity"] = row["equity"]
            if "drawdown" in row:
                filtered_row["drawdown"] = row["drawdown"]
            filtered_equity.append(filtered_row)
        response.equity_curve = filtered_equity

    if response.artifacts_trades_csv:
        response.trade_log = response.artifacts_trades_csv[:500]

    if include_analysis:
        from src.ui_services import build_run_analysis

        analysis = build_run_analysis(
            run_dir,
            symbols=[chart_symbol] if chart_symbol else None,
            include_payload=chart_payload != "summary" or bool(chart_symbol),
            include_symbol_list=chart_symbols_out is not None,
        )
        if chart_symbols_out is not None:
            chart_symbols_out.extend(analysis.get("chart_symbols") or [])
        response.run_stage = analysis.get("run_stage")
        response.run_context = analysis.get("run_context")
        response.price_series = analysis.get("price_series")
        response.indicator_series = analysis.get("indicator_series")
        response.trade_markers = analysis.get("trade_markers")
        response.run_logs = analysis.get("run_logs")

    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_runs_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the runs routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_runs_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )

    if require_auth is None:
        require_auth = host.require_auth

    # Late-access closures for shared host symbols (monkeypatch-safe)
    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h._validate_path_param(value, kind)

    def _host_RUNS_DIR() -> Path:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h.RUNS_DIR

    def _strategy_spec_for_request(request: BacktraderBacktestRequest) -> Dict[str, Any]:
        from src.execution.strategy_design import configured_strategy_intent

        intent = configured_strategy_intent(request.strategy_description)
        template_id = intent["template_id"]
        parameters = intent["parameters"]
        if template_id == "sma_cross":
            strategy_id = f"sma-cross-{parameters['fast_period']}-{parameters['slow_period']}"
        else:
            strategy_id = f"rsi-threshold-{parameters['period']}-{parameters['entry_below']}-{parameters['exit_above']}"
        return {
            "schema_version": "1.0",
            "strategy_id": strategy_id,
            "source_kind": "natural_language_template",
            "template_id": template_id,
            "template_version": "1.0",
            "instrument_ids": [request.instrument_id],
            "bar_policy": {
                "timeframe": "1d",
                "session": "regular",
                "completed_only": True,
                "calendar_id": "us_equity_rth",
                "timezone": "America/New_York",
            },
            "parameters": parameters,
            "position_policy": {
                "mode": "target_fraction",
                "long_only": True,
                "whole_shares": True,
                "allocation_fraction": request.allocation_fraction,
                "rebalance": "on_signal_transition",
                "initial_entry": "wait_for_cross",
            },
            "execution_policy": {
                "decision": "completed_bar_available",
                "dispatch": "next_regular_session",
                "order_type": "market",
                "time_in_force": "DAY",
                "outside_rth": False,
                "gap_action": "skip",
                "missed_session_action": "expire",
            },
        }

    def _strategy_template_identity() -> Dict[str, str]:
        from src.bt_runtime.strategies import sma as sma_runtime

        return {
            "source_sha256": sma_runtime.TRUSTED_TEMPLATE_SOURCE_SHA256,
            "compiler_version": sma_runtime.TRUSTED_TEMPLATE_COMPILER_VERSION,
        }

    def _review_hash_for_request(
        request: BacktraderBacktestRequest,
        strategy_spec: Dict[str, Any],
        template_identity: Dict[str, str],
        market_snapshot: Dict[str, Any],
    ) -> str:
        payload = {
            "request": request.model_dump(exclude={
                "preview_token", "clarification_transcript_id", "clarification_turn_id",
                "clarification_parent_turn_id", "clarification_answer",
            }, mode="json"),
            "strategy_spec": strategy_spec,
            "run_defaults": _BACKTRADER_RUN_DEFAULTS,
            "engine_version": "1.9.78.123",
            "template_identity": template_identity,
            "market_data_sha256": market_snapshot["data_sha256"],
            "data_manifest_sha256": market_snapshot["manifest_sha256"],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _strategy_request_hash(request: BacktraderBacktestRequest) -> str:
        encoded = json.dumps(
            request.model_dump(exclude={
                "preview_token", "clarification_transcript_id", "clarification_turn_id",
                "clarification_parent_turn_id", "clarification_answer",
            }, mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _python_upload_confirmation_request_hash(
        request: PythonUploadBacktestConfirmationRequest,
    ) -> str:
        identity = {
            "strategy_id": request.strategy_id,
            "instrument_id": request.instrument_id,
            "strategy_source": request.strategy_source,
            "initial_cash": request.initial_cash,
            "commission_rate": request.commission_rate,
            "slippage_fraction": request.slippage_fraction,
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _python_upload_snapshot_for_request(request: PythonUploadBacktestRequest) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "schema_version": "python-upload-market-snapshot/1",
            "market_data_csv": request.market_data_csv,
            "market_data_metadata": request.market_data_metadata,
            "expected_sessions": [item.model_dump(mode="json") for item in request.expected_sessions],
        }
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded_bytes = encoded.encode("utf-8")
        if len(encoded_bytes) > _PREVIEW_AUTHORIZATION_MAX_SINGLE_BYTES:
            raise ValueError("Python upload preview snapshot exceeds 80 MiB")
        snapshot["snapshot_sha256"] = hashlib.sha256(encoded_bytes).hexdigest()
        return snapshot

    def _validated_python_upload_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if set(snapshot) != {
            "schema_version", "market_data_csv", "market_data_metadata", "expected_sessions", "snapshot_sha256",
        }:
            raise ValueError("Python upload preview snapshot fields are invalid")
        if snapshot.get("schema_version") != "python-upload-market-snapshot/1":
            raise ValueError("Python upload preview snapshot has an unsupported schema")
        supplied_hash = snapshot.get("snapshot_sha256")
        payload = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if not isinstance(supplied_hash, str) or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != supplied_hash:
            raise ValueError("Python upload preview snapshot integrity check failed")
        if not isinstance(payload.get("market_data_csv"), str) or not isinstance(payload.get("market_data_metadata"), dict):
            raise ValueError("Python upload preview snapshot is incomplete")
        if not isinstance(payload.get("expected_sessions"), list):
            raise ValueError("Python upload preview schedule is incomplete")
        return payload

    def _issue_preview_token(
        review_hash: str,
        *,
        request_hash: Optional[str] = None,
        strategy_spec: Optional[Dict[str, Any]] = None,
        market_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        strategy_spec_json = (
            json.dumps(strategy_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if strategy_spec is not None else None
        )
        market_snapshot_json = (
            json.dumps(market_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if market_snapshot is not None else None
        )
        new_payload_bytes = sum(
            len(value.encode("utf-8")) for value in (strategy_spec_json, market_snapshot_json) if value is not None
        )
        if new_payload_bytes > _PREVIEW_AUTHORIZATION_MAX_SINGLE_BYTES:
            raise HTTPException(status_code=413, detail="preview authorization payload exceeds the storage limit")
        runs_dir = _host_RUNS_DIR()
        runs_dir.mkdir(parents=True, exist_ok=True)
        database = runs_dir / ".backtest_preview_authorizations.sqlite3"
        connection = sqlite3.connect(database, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS preview_authorizations ("
                    "token_hash TEXT PRIMARY KEY, review_hash TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                    "consumed_at INTEGER, request_hash TEXT, strategy_spec_json TEXT, market_snapshot_json TEXT)"
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(preview_authorizations)")}
                if "request_hash" not in columns:
                    connection.execute("ALTER TABLE preview_authorizations ADD COLUMN request_hash TEXT")
                if "strategy_spec_json" not in columns:
                    connection.execute("ALTER TABLE preview_authorizations ADD COLUMN strategy_spec_json TEXT")
                if "market_snapshot_json" not in columns:
                    connection.execute("ALTER TABLE preview_authorizations ADD COLUMN market_snapshot_json TEXT")
                now_epoch = int(time.time())
                connection.execute(
                    "DELETE FROM preview_authorizations WHERE expires_at < ? OR consumed_at IS NOT NULL",
                    (now_epoch,),
                )
                pending_payload_bytes = connection.execute(
                    "SELECT COALESCE(SUM(COALESCE(length(CAST(strategy_spec_json AS BLOB)), 0) + "
                    "COALESCE(length(CAST(market_snapshot_json AS BLOB)), 0)), 0) "
                    "FROM preview_authorizations WHERE expires_at >= ? AND consumed_at IS NULL",
                    (now_epoch,),
                ).fetchone()[0]
                if pending_payload_bytes + new_payload_bytes > _PREVIEW_AUTHORIZATION_MAX_ACTIVE_BYTES:
                    raise HTTPException(
                        status_code=429,
                        detail="too many unconfirmed previews; confirm an existing preview or wait for expiry",
                    )
                connection.execute(
                    "INSERT INTO preview_authorizations(token_hash, review_hash, expires_at, request_hash, "
                    "strategy_spec_json, market_snapshot_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        hashlib.sha256(token.encode("ascii")).hexdigest(),
                        review_hash,
                        now_epoch + 600,
                        request_hash,
                        strategy_spec_json,
                        market_snapshot_json,
                    ),
                )
        finally:
            connection.close()
        try:
            database.chmod(0o600)
        except OSError:
            pass
        return token

    def _load_preview_strategy_spec(token: str | None, request_hash: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        database = _host_RUNS_DIR() / ".backtest_preview_authorizations.sqlite3"
        if not database.is_file():
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        connection = sqlite3.connect(database, timeout=10)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(preview_authorizations)")}
            if not {"request_hash", "strategy_spec_json"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT strategy_spec_json FROM preview_authorizations WHERE token_hash = ? "
                "AND request_hash = ? AND expires_at >= ? AND consumed_at IS NULL",
                (token_hash, request_hash, int(time.time())),
            ).fetchone()
            if row is None or not row[0]:
                return None
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (sqlite3.Error, json.JSONDecodeError):
            return None
        finally:
            connection.close()

    def _load_preview_market_snapshot(token: str | None, request_hash: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        database = _host_RUNS_DIR() / ".backtest_preview_authorizations.sqlite3"
        if not database.is_file():
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        connection = sqlite3.connect(database, timeout=10)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(preview_authorizations)")}
            if not {"request_hash", "market_snapshot_json"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT market_snapshot_json FROM preview_authorizations WHERE token_hash = ? "
                "AND request_hash = ? AND expires_at >= ? AND consumed_at IS NULL",
                (token_hash, request_hash, int(time.time())),
            ).fetchone()
            if row is None or not row[0]:
                return None
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (sqlite3.Error, json.JSONDecodeError):
            return None
        finally:
            connection.close()

    def _consume_preview_token(token: str | None, review_hash: str) -> bool:
        if not token:
            return False
        runs_dir = _host_RUNS_DIR()
        database = runs_dir / ".backtest_preview_authorizations.sqlite3"
        if not database.is_file():
            return False
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        now_epoch = int(time.time())
        connection = sqlite3.connect(database, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM preview_authorizations WHERE token_hash = ? AND review_hash = ? "
                "AND expires_at >= ? AND consumed_at IS NULL",
                (token_hash, review_hash, now_epoch),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            columns = {row[1] for row in connection.execute("PRAGMA table_info(preview_authorizations)")}
            clear_columns = [name for name in ("strategy_spec_json", "market_snapshot_json") if name in columns]
            clear_sql = "".join(f", {name} = NULL" for name in clear_columns)
            cursor = connection.execute(
                f"UPDATE preview_authorizations SET consumed_at = ?{clear_sql} "
                "WHERE token_hash = ? AND consumed_at IS NULL",
                (now_epoch, token_hash),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def _prepare_python_upload(
        request: PythonUploadBacktestRequest,
        *,
        worker_image: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate uploaded source and market data without creating durable run artifacts."""
        import backtrader as bt
        from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv
        from src.bt_runtime.calendars import (
            CALENDAR_ID, CALENDAR_TIMEZONE, EXPECTED_CALENDAR_VERSION, build_xnys_schedule,
        )
        from src.execution.contracts import StrategySpec, canonical_sha256
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.python_sandbox import validate_worker_image_identity
        from src.execution.strategy_source_policy import validate_strategy_source, StrategySourcePolicyError

        metadata = MarketDataMetadata.model_validate(request.market_data_metadata)
        if metadata.instrument_id != request.instrument_id:
            raise ValueError("market-data metadata instrument must match request.instrument_id")
        if (metadata.calendar_id, metadata.calendar_version, metadata.timezone) != (
            CALENDAR_ID, EXPECTED_CALENDAR_VERSION, CALENDAR_TIMEZONE,
        ):
            raise ValueError(
                f"Python upload requires {CALENDAR_ID} calendar {EXPECTED_CALENDAR_VERSION} "
                f"in {CALENDAR_TIMEZONE}"
            )
        canonical_schedule = build_xnys_schedule(metadata.warmup_start, metadata.evaluation_end)
        submitted_sessions = [MarketSession(
            session_date=item.session_date,
            open_at=item.open_at,
            close_at=item.close_at,
            early_close=item.early_close,
        ) for item in request.expected_sessions]
        if submitted_sessions != list(canonical_schedule.sessions):
            raise ValueError("expected_sessions must exactly match the pinned XNYS schedule")
        if (metadata.calendar_schedule_sha256 is not None
                and metadata.calendar_schedule_sha256 != canonical_schedule.sha256):
            raise ValueError("calendar_schedule_sha256 does not match the pinned XNYS schedule")
        raw_csv = request.market_data_csv.encode("utf-8")
        # Import against the host-generated schedule so the manifest hash is
        # rooted in the pinned calendar even when the client omits that hash.
        sessions = list(canonical_schedule.sessions)
        imported = import_completed_daily_csv(raw_csv, metadata, sessions)
        source_bytes = request.strategy_source.encode("utf-8")
        try:
            validate_strategy_source(request.strategy_source)
        except StrategySourcePolicyError as exc:
            raise ValueError(str(exc)) from exc

        source_artifact_id = f"upload-{hashlib.sha256(source_bytes).hexdigest()[:16]}"
        spec = StrategySpec.model_validate({
            "schema_version": "1.0",
            "strategy_id": request.strategy_id,
            "source_kind": "python_upload",
            "source_artifact_id": source_artifact_id,
            "instrument_ids": [request.instrument_id],
            "bar_policy": {
                "timeframe": "1d", "session": "regular", "completed_only": True,
                "calendar_id": metadata.calendar_id, "timezone": metadata.timezone,
            },
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {
                "decision": "completed_bar_available", "dispatch": "next_regular_session",
                "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause",
            },
        })
        engine_version = f"backtrader/{bt.__version__}"
        manifest_hash = canonical_sha256(imported.manifest)
        spec_hash = canonical_sha256(spec)
        input_hash = hashlib.sha256(json.dumps(
            request.model_dump(exclude={"preview_token"}, mode="json"),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        if worker_image is not None:
            worker_image = validate_worker_image_identity(worker_image)
        review_hash = canonical_sha256({
            "input_hash": input_hash,
            "strategy_spec_hash": spec_hash,
            "data_manifest_hash": manifest_hash,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "market_data_sha256": imported.raw_sha256,
            "engine_version": engine_version,
            "compiler_version": PYTHON_WORKER_COMPILER_VERSION,
            "worker_image": worker_image,
        })
        return {
            "metadata": metadata,
            "raw_csv": raw_csv,
            "sessions": sessions,
            "imported": imported,
            "source_bytes": source_bytes,
            "spec": spec,
            "engine_version": engine_version,
            "compiler_version": PYTHON_WORKER_COMPILER_VERSION,
            "worker_image": worker_image,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "manifest_hash": manifest_hash,
            "spec_hash": spec_hash,
            "review_hash": review_hash,
        }

    # Pydantic models for response_model (resolved at registration time)
    RunResponse = host.RunResponse
    RunInfo = host.RunInfo

    # --- Routes ---

    @app.post("/runs/backtests/preview", dependencies=[Depends(require_auth)])
    async def preview_backtrader_backtest(request: BacktraderBacktestRequest):
        """Normalize and review a trusted template without creating a run or version."""
        from src.execution.strategy_clarification_transcripts import (
            ClarificationTranscriptError,
            StrategyClarificationTranscriptStore,
        )

        runs_dir = _host_RUNS_DIR()
        transcript_store = StrategyClarificationTranscriptStore(
            runs_dir / ".strategy_clarification_transcripts.sqlite3",
        )
        transcript_context = {
            "symbol": request.symbol,
            "instrument_id": request.instrument_id,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "source": request.source,
            "ibkr_profile_id": request.ibkr_profile_id,
            "ibkr_port": request.ibkr_port,
            "allocation_fraction": request.allocation_fraction,
        }
        transcript_context_sha256 = hashlib.sha256(json.dumps(
            transcript_context, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        try:
            clarification_fields = (
                request.clarification_transcript_id,
                request.clarification_turn_id,
                request.clarification_parent_turn_id,
                request.clarification_answer,
            )
            if request.clarification_transcript_id is None:
                if any(value is not None for value in clarification_fields[1:]):
                    raise ClarificationTranscriptError("clarification turn fields require a transcript ID")
                transcript = transcript_store.append_initial(
                    request.strategy_description, context_sha256=transcript_context_sha256,
                )
            else:
                if any(value is None for value in clarification_fields[1:]):
                    raise ClarificationTranscriptError("clarification turn metadata is incomplete")
                transcript = transcript_store.append_clarification(
                    transcript_id=request.clarification_transcript_id,
                    turn_id=request.clarification_turn_id,
                    parent_turn_id=request.clarification_parent_turn_id,
                    answer=request.clarification_answer,
                    submitted_description=request.strategy_description,
                    context_sha256=transcript_context_sha256,
                )
        except ClarificationTranscriptError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        transcript_cursor = {
            "transcript_id": transcript.transcript_id,
            "turn_id": transcript.turn_id,
            "turn_count": transcript.turn_count,
        }
        try:
            strategy_spec = await run_in_threadpool(_strategy_spec_for_request, request)
        except ValueError as exc:
            from src.execution.strategy_design import clarification_form_for_error

            clarification_form = clarification_form_for_error(request.strategy_description, str(exc))
            return {
                "supported": False,
                "clarification": str(exc),
                "reason": str(exc),
                **({"clarification_form": clarification_form} if clarification_form is not None else {}),
                "clarification_transcript": transcript_cursor,
            }
        try:
            from backtest.runner import prepare_trusted_provider_market_snapshot
            from src.execution.contracts import StrategySpec

            snapshot_config = {
                "codes": [request.symbol],
                "instrument_id": request.instrument_id,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "source": request.source,
                "ibkr_profile_id": request.ibkr_profile_id,
                "ibkr_port": request.ibkr_port,
                "interval": "1D",
            }
            market_snapshot = await run_in_threadpool(
                prepare_trusted_provider_market_snapshot,
                snapshot_config,
                StrategySpec.model_validate(strategy_spec),
            )
        except ValueError as exc:
            return {
                "supported": False, "clarification": str(exc), "reason": str(exc),
                "clarification_transcript": transcript_cursor,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Market data preview failed: {type(exc).__name__}",
            ) from exc
        template_identity = _strategy_template_identity()
        review_hash = _review_hash_for_request(request, strategy_spec, template_identity, market_snapshot)
        return {
            "supported": True,
            "review_hash": review_hash,
            "preview_token": _issue_preview_token(
                review_hash,
                request_hash=_strategy_request_hash(request),
                strategy_spec=strategy_spec,
                market_snapshot=market_snapshot,
            ),
            "template_source_sha256": template_identity["source_sha256"],
            "compiler_version": template_identity["compiler_version"],
            "clarification_transcript": transcript_cursor,
            "strategy_spec": strategy_spec,
            "summary": {
                "symbol": request.symbol,
                "instrument_id": request.instrument_id,
                "strategy_description": request.strategy_description,
                "source": request.source,
                "ibkr_profile_id": request.ibkr_profile_id,
                "ibkr_port": request.ibkr_port,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "allocation_fraction": request.allocation_fraction,
                **{key: _BACKTRADER_RUN_DEFAULTS[key] for key in (
                    "strategy_budget", "fee_reserve", "price_buffer_fraction", "commission_rate", "slippage_fraction", "initial_cash",
                )},
                "market_data_sha256": market_snapshot["data_sha256"],
                "data_manifest_sha256": market_snapshot["manifest_sha256"],
                "market_data_provider": market_snapshot["provider"],
                "market_data_bar_count": market_snapshot["bar_count"],
            },
            "warnings": [
                "Natural-language extraction can miss conditions; compare the original description with the explicit rules before confirming.",
                "The exact CSV snapshot is frozen for confirmation and execution; provider claims, point-in-time availability, calendar authorization, adjustments, and symbol mapping remain unverified.",
                "A supported offline preview does not authorize broker orders.",
            ],
        }

    @app.post("/runs/backtests", dependencies=[Depends(require_auth)])
    async def create_backtrader_backtest(request: BacktraderBacktestRequest):
        """Run exactly the bounded trusted template reviewed in a fresh preview."""
        from src.execution.strategy_design import known_unsupported_condition

        unsupported = known_unsupported_condition(request.strategy_description)
        if unsupported:
            from src.execution.strategy_design import parse_fixed_rsi_description

            has_reviewed_rsi_candidate = bool(
                re.search(r"(?<![A-Za-z0-9])(?:rsi|relative strength(?: index)?)(?![A-Za-z0-9])|相对强弱", request.strategy_description, re.I)
            ) and bool(request.preview_token)
            try:
                parse_fixed_rsi_description(request.strategy_description)
                is_fixed_rsi = True
            except ValueError:
                is_fixed_rsi = False
            if not is_fixed_rsi and not has_reviewed_rsi_candidate:
                raise HTTPException(
                    status_code=422,
                    detail=f"condition '{unsupported}' is not supported by the trusted template",
                )
        strategy_spec = _load_preview_strategy_spec(request.preview_token, _strategy_request_hash(request))
        market_snapshot = _load_preview_market_snapshot(request.preview_token, _strategy_request_hash(request))
        if strategy_spec is None or market_snapshot is None:
            raise HTTPException(status_code=409, detail="a fresh strategy preview and confirmation are required")
        template_identity = _strategy_template_identity()
        review_hash = _review_hash_for_request(request, strategy_spec, template_identity, market_snapshot)
        if not _consume_preview_token(request.preview_token, review_hash):
            raise HTTPException(status_code=409, detail="a fresh strategy preview and confirmation are required")
        now = datetime.now(timezone.utc)
        run_id = f"bt_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
        runs_dir = _host_RUNS_DIR()
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = runs_dir / run_id
        # mkdir is exclusive: a collision cannot overwrite a prior run.
        run_dir.mkdir()
        config: Dict[str, Any] = {
            "codes": [request.symbol],
            "instrument_id": request.instrument_id,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "source": request.source,
            "ibkr_profile_id": request.ibkr_profile_id,
            "ibkr_port": request.ibkr_port,
            **_BACKTRADER_RUN_DEFAULTS,
            "expected_strategy_source_hash": template_identity["source_sha256"],
            "expected_compiler_version": template_identity["compiler_version"],
            "strategy_spec": strategy_spec,
        }
        from src.bt_runtime.report_artifact_integrity import SCHEMA_VERSION as REPORT_INTEGRITY_SCHEMA

        config["execution_report_integrity_schema"] = REPORT_INTEGRITY_SCHEMA
        (run_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        (run_dir / "req.json").write_text(
            json.dumps(
                {
                    "prompt": f"{request.strategy_description.strip()} ({request.symbol})",
                    "context": {
                        "codes": [request.symbol],
                        "start_date": request.start_date,
                        "end_date": request.end_date,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (run_dir / "state.json").write_text(
            json.dumps({"status": "running", "created_at": now.isoformat()}), encoding="utf-8"
        )
        # Keep the reviewed snapshot out of public config.json, but pass the
        # exact same bytes and manifest to the trusted runner.
        config["_frozen_market_data"] = market_snapshot
        try:
            # Import lazily so the route remains lightweight and tests can
            # replace the provider-backed runner without importing loaders.
            from backtest.runner import run_backtrader_backend

            report = await run_in_threadpool(run_backtrader_backend, config, run_dir)
            from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar

            write_report_integrity_sidecar(run_dir / "artifacts" / "execution_report.json")
        except ValueError as exc:
            (run_dir / "state.json").write_text(
                json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
                encoding="utf-8",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            (run_dir / "state.json").write_text(
                json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
                encoding="utf-8",
            )
            raise HTTPException(
                status_code=502,
                detail=f"Backtest provider or engine failed: {type(exc).__name__}",
            ) from exc
        (run_dir / "state.json").write_text(
            json.dumps({"status": "success", "created_at": now.isoformat()}), encoding="utf-8"
        )
        return {"run_id": run_id, "status": "success", "execution_report": report}

    @app.post("/runs/market-data/ibkr/python-upload-preview", dependencies=[Depends(require_auth)])
    async def preview_ibkr_market_data_for_python_upload(request: IbkrPythonMarketDataRequest):
        """Fetch read-only IBKR daily bars and format them for the upload preview."""
        from src.bt_runtime.calendars import (
            CALENDAR_ID, CALENDAR_TIMEZONE, EXPECTED_CALENDAR_VERSION, build_xnys_schedule,
        )
        from backtest.runner import fetch_exact_ibkr_daily

        try:
            schedule = build_xnys_schedule(request.warmup_start, request.evaluation_end)
            sessions = list(schedule.sessions)
            session_dates = [item.session_date for item in sessions]
            if len(sessions) > 1000:
                raise ValueError("IBKR Python upload is limited to 1,000 daily bars per run")
            required_dates = {
                request.warmup_start, request.warmup_end,
                request.evaluation_start, request.evaluation_end,
            }
            if not required_dates.issubset(set(session_dates)):
                raise ValueError("all four range boundaries must be XNYS trading sessions")

            fetched = await run_in_threadpool(
                fetch_exact_ibkr_daily,
                request.symbol,
                request.warmup_start,
                request.evaluation_end,
                instrument_id=request.instrument_id,
                profile_id=request.profile_id,
                **({"port": request.port} if request.port is not None else {}),
            )
            if fetched.symbol != request.symbol:
                raise ValueError("IBKR provider returned a different symbol")
            frame = fetched.frame
            returned_dates = [stamp.date() for stamp in frame.index]
            if returned_dates != session_dates:
                missing = sorted(set(session_dates) - set(returned_dates))
                unexpected = sorted(set(returned_dates) - set(session_dates))
                raise ValueError(
                    f"IBKR bars must cover every requested XNYS session; "
                    f"missing={missing[:5]}, unexpected={unexpected[:5]}"
                )

            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(("session_date", "start_at", "end_at", "available_at", "open", "high", "low", "close", "volume"))
            for session, (_, row) in zip(sessions, frame.iterrows(), strict=True):
                open_at = session.open_at.isoformat()
                close_at = session.close_at.isoformat()
                writer.writerow((
                    session.session_date.isoformat(), open_at, close_at, close_at,
                    *(format(float(row[key]), ".17g") for key in ("open", "high", "low", "close", "volume")),
                ))
            market_data_csv = output.getvalue()
            if len(market_data_csv.encode("utf-8")) > 64 * 1024 * 1024:
                raise ValueError("IBKR market-data CSV exceeds 64 MiB")

            expected_sessions = [{
                "session_date": item.session_date.isoformat(),
                "open_at": item.open_at.isoformat(),
                "close_at": item.close_at.isoformat(),
                "early_close": item.early_close,
            } for item in sessions]
            metadata = {
                "provider": "ibkr",
                "instrument_id": request.instrument_id,
                "currency": "USD",
                "timezone": CALENDAR_TIMEZONE,
                "calendar_id": CALENDAR_ID,
                "calendar_version": EXPECTED_CALENDAR_VERSION,
                "calendar_schedule_sha256": schedule.sha256,
                "availability_model": "completed_bar_available_at_xnys_close_publication_unverified",
                "adjustment_policy": "IBKR_TRADES_adjustment_semantics_unverified",
                "corporate_action_policy": "IBKR_corporate_action_treatment_unverified",
                "fetched_at": fetched.provenance["retrieved_at"],
                "warmup_start": request.warmup_start.isoformat(),
                "warmup_end": request.warmup_end.isoformat(),
                "evaluation_start": request.evaluation_start.isoformat(),
                "evaluation_end": request.evaluation_end.isoformat(),
                "download_parameters": fetched.provenance,
            }
            return {
                "supported": True,
                "symbol": request.symbol,
                "bar_count": len(frame),
                "market_data_csv": market_data_csv,
                "market_data_metadata": metadata,
                "expected_sessions": expected_sessions,
                "provenance": fetched.provenance,
                "warnings": [
                    "IBKR local API profile is asserted read-only; fetched data is frozen at the subsequent upload preview.",
                    "Historical publication timing and adjustment semantics remain unverified.",
                ],
            }
        except (ValueError, TypeError) as exc:
            return {"supported": False, "reason": str(exc)}
        except Exception as exc:
            return {"supported": False, "reason": f"IBKR history request failed: {type(exc).__name__}"}

    @app.post("/runs/backtests/python-upload/preview", dependencies=[Depends(require_auth)])
    async def preview_python_upload_backtest(request: PythonUploadBacktestRequest):
        """Validate source and imported data, then bind a short-lived confirmation token."""
        image = os.environ.get("VIBE_PYTHON_WORKER_IMAGE", "").strip()
        try:
            prepared = _prepare_python_upload(request, worker_image=image or None)
            worker_configured = prepared["worker_image"] is not None
            frozen_upload_snapshot = _python_upload_snapshot_for_request(request) if worker_configured else None
        except (ValueError, TypeError) as exc:
            return {"supported": False, "reason": str(exc)}
        token = None
        manifest = prepared["imported"].manifest
        if worker_configured:
            assert frozen_upload_snapshot is not None
            token = _issue_preview_token(
                prepared["review_hash"],
                request_hash=_python_upload_confirmation_request_hash(
                    PythonUploadBacktestConfirmationRequest(
                        strategy_id=request.strategy_id,
                        instrument_id=request.instrument_id,
                        strategy_source=request.strategy_source,
                        initial_cash=request.initial_cash,
                        commission_rate=request.commission_rate,
                        slippage_fraction=request.slippage_fraction,
                    )
                ),
                market_snapshot=frozen_upload_snapshot,
            )
        return {
            "supported": True,
            "can_run": worker_configured,
            "worker_image_configured": worker_configured,
            "review_hash": prepared["review_hash"],
            "preview_token": token,
            "backend": "backtrader",
            "mode": "backtest",
            "engine_version": prepared["engine_version"],
            "compiler_version": prepared["compiler_version"],
            "worker_image": prepared["worker_image"],
            "strategy_spec": prepared["spec"].model_dump(mode="json"),
            "strategy_spec_hash": prepared["spec_hash"],
            "source_sha256": prepared["source_sha256"],
            "market_data_sha256": prepared["imported"].raw_sha256,
            "data_manifest_hash": prepared["manifest_hash"],
            "data_manifest": manifest.model_dump(mode="json"),
            "summary": {
                "strategy_id": request.strategy_id,
                "instrument_id": request.instrument_id,
                "provider": manifest.provider,
                "calendar": manifest.calendar_version,
                "bar_count": len(prepared["imported"].bar_records),
                "range_start": manifest.range_start.isoformat(),
                "range_end": manifest.range_end.isoformat(),
                "warmup_start": manifest.warmup_start.isoformat(),
                "warmup_end": manifest.warmup_end.isoformat(),
                "evaluation_start": manifest.evaluation_start.isoformat(),
                "evaluation_end": manifest.evaluation_end.isoformat(),
                "initial_cash": request.initial_cash,
                "commission_rate": request.commission_rate,
                "slippage_fraction": request.slippage_fraction,
                "data_authenticity": manifest.quality_report.get("provider_attestation", "not_attested"),
                "adjustment_attestation": manifest.quality_report.get("adjustment_attestation", "not_attested"),
            },
            "warnings": [
                "Provider, calendar and adjustment labels are caller-declared and are not externally authenticated.",
                "This confirmation authorizes an offline backtest only; it does not authorize broker access or orders.",
            ] + ([] if worker_configured else ["The isolated Python worker image is not configured; this upload cannot run yet."]),
        }

    @app.post("/runs/backtests/python-upload", dependencies=[Depends(require_auth)])
    async def create_python_upload_backtest(request: PythonUploadBacktestConfirmationRequest):
        """Run the exact source/data/parameters reviewed in a fresh upload preview."""
        image = os.environ.get("VIBE_PYTHON_WORKER_IMAGE", "").strip()
        if not image:
            raise HTTPException(status_code=503, detail="Python strategy worker image is not configured")
        if not request.preview_token:
            raise HTTPException(status_code=409, detail="a fresh Python upload preview and confirmation are required")
        snapshot = _load_preview_market_snapshot(
            request.preview_token,
            _python_upload_confirmation_request_hash(request),
        )
        if snapshot is None:
            raise HTTPException(status_code=409, detail="a fresh Python upload preview and confirmation are required")
        try:
            frozen_snapshot = _validated_python_upload_snapshot(snapshot)
            request = PythonUploadBacktestRequest.model_validate({
                "strategy_id": request.strategy_id,
                "instrument_id": request.instrument_id,
                "strategy_source": request.strategy_source,
                "initial_cash": request.initial_cash,
                "commission_rate": request.commission_rate,
                "slippage_fraction": request.slippage_fraction,
                "market_data_csv": frozen_snapshot["market_data_csv"],
                "market_data_metadata": frozen_snapshot["market_data_metadata"],
                "expected_sessions": frozen_snapshot["expected_sessions"],
                "preview_token": request.preview_token,
            })
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="Python upload preview snapshot failed integrity validation") from exc
        try:
            from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
            from src.execution.python_sandbox import PythonSandbox
            from src.execution.versions import StrategyVersionStore

            prepared = _prepare_python_upload(request, worker_image=image)
            sandbox = PythonSandbox(image)  # Validate immutable image identity before consuming the token.
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not _consume_preview_token(request.preview_token, prepared["review_hash"]):
            raise HTTPException(status_code=409, detail="Python upload inputs changed or the preview token expired")

        now = datetime.now(timezone.utc)
        run_id = f"btpy_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
        runs_dir = _host_RUNS_DIR()
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = runs_dir / run_id
        run_dir.mkdir()
        imported = prepared["imported"]
        metadata = prepared["metadata"]
        from src.bt_runtime.report_artifact_integrity import SCHEMA_VERSION as REPORT_INTEGRITY_SCHEMA

        (run_dir / "config.json").write_text(json.dumps({
            "execution_backend": "backtrader",
            "execution_mode": "backtest",
            "source_kind": "python_upload",
            "codes": [request.instrument_id],
            "instrument_id": request.instrument_id,
            "strategy_id": request.strategy_id,
            "start_date": imported.manifest.range_start.isoformat(),
            "end_date": imported.manifest.range_end.isoformat(),
            "source": metadata.provider,
            "data_authenticity": "caller_declared_unverified",
            "preview_review_hash": prepared["review_hash"],
            "worker_image": prepared["worker_image"],
            "initial_cash": request.initial_cash,
            "commission_rate": request.commission_rate,
            "slippage_fraction": request.slippage_fraction,
            "execution_report_integrity_schema": REPORT_INTEGRITY_SCHEMA,
        }, sort_keys=True), encoding="utf-8")
        (run_dir / "req.json").write_text(json.dumps({
            "prompt": f"Python upload backtest: {request.strategy_id} ({request.instrument_id})",
            "context": {
                "source_kind": "python_upload", "instrument_id": request.instrument_id,
                "codes": [request.instrument_id],
                "start_date": imported.manifest.range_start.isoformat(),
                "end_date": imported.manifest.range_end.isoformat(),
            },
        }, sort_keys=True), encoding="utf-8")
        (run_dir / "state.json").write_text(json.dumps({"status": "running", "created_at": now.isoformat()}), encoding="utf-8")
        try:
            from backtrader import __version__ as backtrader_version
            from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
            from src.execution.contracts import canonical_sha256

            config_path = run_dir / "config.json"
            run_config = json.loads(config_path.read_text(encoding="utf-8"))
            run_config.update({
                "data_manifest_hash": canonical_sha256(imported.manifest),
                "market_data_file_sha256": imported.raw_sha256,
                "strategy_spec_hash": prepared["spec_hash"],
                "strategy_source_sha256": prepared["source_sha256"],
            })
            config_path.write_text(json.dumps(run_config, sort_keys=True), encoding="utf-8")
            versions = StrategyVersionStore(run_dir.parent.parent / "strategy-versions")
            version = versions.create_version(
                prepared["spec"],
                engine_version=f"backtrader/{backtrader_version}",
                compiler_version=PYTHON_WORKER_COMPILER_VERSION,
                upload_source=prepared["source_bytes"],
            )
            report = await run_in_threadpool(
                run_python_upload_service,
                version_id=version.version_id,
                versions=versions,
                market_data=imported,
                raw_csv=prepared["raw_csv"],
                sandbox=sandbox,
                run_dir=run_dir,
                initial_cash=request.initial_cash,
                commission_rate=request.commission_rate,
                slippage_fraction=request.slippage_fraction,
                run_id=run_id,
                resume_context={
                    "market_data_metadata": metadata.model_dump(mode="json"),
                    "expected_sessions": [session.model_dump(mode="json") for session in request.expected_sessions],
                },
                worker_image=prepared["worker_image"],
            )
            from src.bt_runtime.report_artifact_integrity import write_report_integrity_sidecar

            write_report_integrity_sidecar(run_dir / "artifacts" / "execution_report.json")
        except (ValueError, TypeError) as exc:
            (run_dir / "state.json").write_text(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), encoding="utf-8")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            safe_failure = _public_python_strategy_failure(exc)
            if safe_failure is not None:
                (run_dir / "state.json").write_text(
                    json.dumps(safe_failure, ensure_ascii=False, sort_keys=True), encoding="utf-8"
                )
                raise HTTPException(status_code=422, detail=safe_failure["message"]) from exc
            (run_dir / "state.json").write_text(json.dumps({"status": "failed", "error": type(exc).__name__}), encoding="utf-8")
            raise HTTPException(status_code=502, detail=f"Isolated Backtrader run failed: {type(exc).__name__}") from exc
        (run_dir / "state.json").write_text(json.dumps({"status": "success", "created_at": now.isoformat()}), encoding="utf-8")
        return {"run_id": run_id, "status": "success", "execution_report": report}

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(require_auth)])
    async def resume_python_upload_backtest(run_id: str):
        """Resume a failed Python upload backtest from its durable event journal."""
        _host_validate_path_param(run_id, "run_id")
        run_dir = _host_RUNS_DIR() / run_id
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="Backtest run not found")
        config_path = run_dir / "config.json"
        state_path = run_dir / "state.json"
        if not config_path.exists() or not state_path.exists():
            raise HTTPException(status_code=409, detail="Run metadata is incomplete and cannot be resumed")
        run_config = json.loads(config_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not (
            run_config.get("execution_backend") == "backtrader"
            and run_config.get("execution_mode") == "backtest"
            and run_config.get("source_kind") == "python_upload"
        ):
            raise HTTPException(status_code=409, detail="Only Python upload Backtrader backtests can be resumed")
        if state.get("status") not in {"running", "failed"}:
            raise HTTPException(status_code=409, detail="Only incomplete backtest runs can be resumed")
        image = os.environ.get("VIBE_PYTHON_WORKER_IMAGE", "").strip()
        if not image:
            raise HTTPException(status_code=503, detail="Python strategy worker image is not configured")
        try:
            from src.bt_runtime.strategies.python_upload_service import (
                load_python_upload_resume_inputs,
                run_python_upload_service,
            )
            from src.bt_runtime.backtest_event_journal import BacktestJournalError
            from src.execution.python_sandbox import PythonSandbox
            from src.execution.strategy_source_policy import StrategySourcePolicyError, validate_strategy_source
            from src.execution.versions import StrategyVersionStore

            saved = load_python_upload_resume_inputs(run_dir)
            if run_config.get("worker_image") != image:
                raise HTTPException(status_code=409, detail="configured Python worker image differs from the frozen run")
            sandbox = PythonSandbox(image)
            versions = StrategyVersionStore(run_dir.parent.parent / "strategy-versions")
            version = versions.get_version(saved["version_id"])
            try:
                source_text = versions.read_upload_source(version.version_id).decode("utf-8", errors="strict")
                validate_strategy_source(source_text)
            except (UnicodeDecodeError, StrategySourcePolicyError) as exc:
                raise ValueError("frozen strategy source is outside the supported upload policy") from exc
            if (
                version.version_hash != saved["strategy_version_hash"]
                or version.spec_hash != saved["strategy_spec_hash"]
            ):
                raise ValueError("frozen strategy version does not match the persisted backtest run")
            state_path.write_text(json.dumps({"status": "running", "resumed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
            report = await run_in_threadpool(
                run_python_upload_service,
                version_id=version.version_id,
                versions=versions,
                market_data=saved["market_data"],
                raw_csv=saved["raw_csv"],
                sandbox=sandbox,
                run_dir=run_dir,
                run_id=run_id,
                initial_cash=saved["initial_cash"],
                commission_rate=saved["commission_rate"],
                slippage_fraction=saved["slippage_fraction"],
                resume_context=saved["resume_context"],
                worker_image=image,
            )
        except HTTPException:
            raise
        except BacktestJournalError as exc:
            state_path.write_text(json.dumps({"status": "paused", "error": str(exc)}, ensure_ascii=False), encoding="utf-8")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            state_path.write_text(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), encoding="utf-8")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            safe_failure = _public_python_strategy_failure(exc)
            if safe_failure is not None:
                state_path.write_text(
                    json.dumps(safe_failure, ensure_ascii=False, sort_keys=True), encoding="utf-8"
                )
                raise HTTPException(status_code=422, detail=safe_failure["message"]) from exc
            state_path.write_text(json.dumps({"status": "failed", "error": type(exc).__name__}), encoding="utf-8")
            raise HTTPException(status_code=502, detail=f"Backtest recovery failed: {type(exc).__name__}") from exc
        state_path.write_text(json.dumps({"status": "success", "resumed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        return {"run_id": run_id, "status": "success", "execution_report": report}

    @app.get("/runs/{run_id}/code", dependencies=[Depends(require_auth)])
    async def get_run_code(run_id: str):
        """Return strategy source files for a run.

        Args:
            run_id: Run identifier.

        Returns:
            Map filename -> source text.
        """
        _host_validate_path_param(run_id, "run_id")
        run_dir = _host_RUNS_DIR() / run_id / "code"
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail=f"Code directory for run {run_id} not found")
        result = {}
        for f in ["signal_engine.py"]:
            p = run_dir / f
            if p.exists():
                result[f] = p.read_text(encoding="utf-8")
        return result

    @app.get("/runs/{run_id}/pine", dependencies=[Depends(require_auth)])
    async def get_run_pine(run_id: str):
        """Return Pine Script file for a run.

        Args:
            run_id: Run identifier.

        Returns:
            Object with pine script content and exists flag.
        """
        _host_validate_path_param(run_id, "run_id")
        pine_path = _host_RUNS_DIR() / run_id / "artifacts" / "strategy.pine"
        if not pine_path.exists():
            return {"exists": False, "content": None}
        return {
            "exists": True,
            "content": pine_path.read_text(encoding="utf-8"),
        }

    @app.get("/runs/{run_id}/factor", dependencies=[Depends(require_auth)])
    async def get_run_factor(run_id: str):
        """Return factor-analysis artifacts for a run.

        Args:
            run_id: Run identifier.

        Returns:
            Object with exists flag, factor results sorted by name, and the
            pairwise IC correlation matrix (null when not computable).
        """
        _host_validate_path_param(run_id, "run_id")
        run_dir = _host_RUNS_DIR() / run_id
        if not run_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found"
            )
        factors = await run_in_threadpool(_scan_factor_results, run_dir)
        if not factors:
            return {"exists": False, "factors": [], "ic_correlation": None}
        return {
            "exists": True,
            "factors": factors,
            "ic_correlation": _factor_ic_correlation(factors),
        }

    @app.get("/runs/{run_id}", response_model=RunResponse, dependencies=[Depends(require_auth)])
    async def get_run_result(
        run_id: str,
        chart_symbol: Optional[str] = Query(None, description="Opt in to chart payloads for a single symbol"),
        chart_payload: Optional[str] = Query(
            None,
            description="Optional chart payload mode. Use 'summary' to omit chart rows and trade markers.",
        ),
    ):
        """Fetch details for a historical run by ``run_id``.

        The default response stays unchanged for existing consumers. Chart-heavy
        optimizations are opt-in via query parameters.
        """
        _host_validate_path_param(run_id, "run_id")
        if chart_payload not in (None, "summary"):
            raise HTTPException(status_code=400, detail="invalid chart_payload")
        run_dir = _host_RUNS_DIR() / run_id

        if not run_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found"
            )

        wants_chart_meta = bool(chart_payload or chart_symbol)
        chart_symbols: List[str] = []
        response = _build_response_from_run_dir(
            run_dir,
            elapsed=0.0,
            include_analysis=True,
            chart_symbol=chart_symbol,
            chart_payload=chart_payload or "full",
            chart_symbols_out=chart_symbols if wants_chart_meta else None,
        )

        integrity = response.execution_report_integrity or {}
        if integrity.get("status") == "invalid":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "execution_report_integrity_failed",
                    "status": "invalid",
                    "error": integrity.get("error", "execution report integrity validation failed"),
                },
            )

        if wants_chart_meta:
            payload = _run_response_payload(response)
            payload["chart_symbols"] = chart_symbols
            return JSONResponse(payload)

        return response

    @app.get("/runs/{run_id}/positions/sectors", dependencies=[Depends(require_auth)])
    async def get_run_positions_sectors(
        run_id: str,
        refresh: int = Query(0, description="Set to 1 to bypass the disk cache and recompute."),
    ):
        """Resolve asset class + A-share industry for a run's position symbols.

        Read-only classification over ``artifacts/positions.csv``, cached in
        ``artifacts/sector_map.json``. The blocking classification (file reads
        plus throttled Eastmoney lookups) runs in the FastAPI threadpool so it
        never stalls the event loop, and is bounded by
        ``_POSITIONS_SECTOR_MAX_SYMBOLS`` / ``_POSITIONS_SECTOR_WORKERS`` /
        the shared client's 15-second per-request timeout.

        Args:
            run_id: Run identifier.
            refresh: ``1`` bypasses the disk cache, ``0`` (default) serves it.

        Returns:
            The sector-map envelope built by ``_build_positions_sector_map``.

        Raises:
            HTTPException: 404 when the run directory does not exist.
        """
        _host_validate_path_param(run_id, "run_id")
        run_dir = _host_RUNS_DIR() / run_id

        if not run_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found"
            )

        return await run_in_threadpool(
            _build_positions_sector_map, run_dir, run_id, refresh=bool(refresh)
        )

    @app.get("/runs", response_model=List[RunInfo], dependencies=[Depends(require_auth)])
    async def list_runs(limit: int = 20):
        """List recent runs with summary fields."""
        from src.ui_services import load_run_context

        limit = min(max(1, limit), 100)
        runs_dir = _host_RUNS_DIR()

        if not runs_dir.exists():
            return []

        run_dirs = sorted(
            [d for d in runs_dir.iterdir() if d.is_dir()],
            key=lambda x: x.name,
            reverse=True
        )

        results = []
        for d in run_dirs[:limit]:
            run_id = d.name

            # Status from state.json or artifacts
            status_val = "unknown"
            state_file = _load_json_file(d / "state.json")
            if state_file:
                status_val = str(state_file.get("status") or "unknown").lower()
            elif (d / "artifacts" / "equity.csv").exists():
                status_val = "success"
            elif (d / "review_report.json").exists():
                status_val = "success"

            # Parse created_at from run_id (YYYYMMDD_HHMMSS or run_YYYYMMDD_HHMMSS)
            created_at = "Unknown"
            if run_id.startswith("run_"):
                parts = run_id.split('_')
                if len(parts) >= 3:
                    d_str, t_str = parts[1], parts[2]
                    if len(d_str) == 8 and len(t_str) == 6:
                        created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"
            elif "_" in run_id:
                parts = run_id.split('_')
                if len(parts) >= 2:
                    d_str, t_str = parts[0], parts[1]
                    if len(d_str) == 8 and len(t_str) == 6:
                        created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"

            if created_at == "Unknown":
                mtime = datetime.fromtimestamp(d.stat().st_mtime)
                created_at = mtime.strftime("%Y-%m-%d %H:%M:%S")

            prompt = None
            req_file = d / "req.json"
            planner_file = d / "planner_output.json"
            if req_file.exists():
                try:
                    req_data = json.loads(req_file.read_text(encoding="utf-8"))
                    prompt = req_data.get("prompt")
                except (json.JSONDecodeError, OSError):
                    pass

            if not prompt and planner_file.exists():
                try:
                    planner_data = json.loads(planner_file.read_text(encoding="utf-8"))
                    prompt = planner_data.get("user_goal") or planner_data.get("goal")
                except (json.JSONDecodeError, OSError):
                    pass

            if not prompt:
                prompt_file = d / "user_prompt.txt"
                if prompt_file.exists():
                    prompt = prompt_file.read_text(encoding="utf-8").strip()

            total_return = None
            sharpe = None
            metrics_file = d / "artifacts" / "metrics.csv"
            if metrics_file.exists():
                try:
                    with open(metrics_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            total_return = float(row.get('total_return', 0) or 0)
                            sharpe = float(row.get('sharpe', 0) or 0)
                            break
                except (OSError, ValueError):
                    pass

            run_context = load_run_context(d)
            results.append(RunInfo(
                run_id=run_id,
                status=status_val,
                created_at=created_at,
                prompt=prompt or "Manual Analysis",
                total_return=total_return,
                sharpe=sharpe,
                codes=run_context.get("codes") or [],
                start_date=run_context.get("start_date"),
                end_date=run_context.get("end_date"),
            ))

        return results
