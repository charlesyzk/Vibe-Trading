"""Rebuild a PAPER strategy checkpoint from a verified backtest's past bars."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from src.bt_runtime.strategies.python_upload_service import load_python_upload_resume_inputs
from src.execution.contracts import Bar, canonical_sha256, is_sha256
from src.execution.event_worker import invoke_strategy_worker
from src.execution.versions import StrategyVersionStore


@dataclass(frozen=True, slots=True)
class PaperWarmup:
    checkpoint: dict[str, Any]
    signal_bar: Bar
    data_manifest_hash: str


def verify_current_signal_bar(*, signal_bar: Bar, gateway_bars: tuple[Bar, ...]) -> Bar:
    """Match the backtest signal against the latest qualified Gateway session.

    Provenance labels and availability timestamps differ between the replay and
    historical adapters; the market values and session identity must agree.
    """
    if not isinstance(signal_bar, Bar) or not signal_bar.is_final:
        raise ValueError("backtest signal must be a completed bar")
    if not gateway_bars or any(not isinstance(bar, Bar) for bar in gateway_bars):
        raise ValueError("qualified Gateway history is unavailable")
    if any(a.session_date >= b.session_date for a, b in zip(gateway_bars, gateway_bars[1:])):
        raise ValueError("Gateway sessions must be strictly ordered")
    latest = gateway_bars[-1]
    if latest.session_date != signal_bar.session_date:
        raise ValueError("backtest signal is not the latest completed Gateway session")
    fields = ("instrument_id", "session_date", "start_at", "end_at", "timezone", "currency",
              "open", "high", "low", "close", "volume", "is_final")
    if any(getattr(signal_bar, field) != getattr(latest, field) for field in fields):
        raise ValueError("backtest signal differs from the qualified Gateway bar")
    return latest


def build_paper_warmup(*, runtime_root: str | Path, run_id: str, version_id: str,
                       expected_manifest_hash: str, sandbox) -> PaperWarmup:
    """Replay all verified bars before the final backtest signal without dispatch."""
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
        raise ValueError("invalid warmup run ID")
    if not isinstance(version_id, str) or not is_sha256(version_id):
        raise ValueError("invalid warmup strategy version")
    root = Path(runtime_root)
    inputs = load_python_upload_resume_inputs(root / "runs" / run_id)
    market = inputs["market_data"]
    if (inputs["version_id"] != version_id or inputs["strategy_version_hash"] != version_id
            or canonical_sha256(market.manifest) != expected_manifest_hash):
        raise ValueError("warmup input differs from the selected strategy or data manifest")
    records = tuple(market.bar_records)
    if not records or records[-1].session_date != market.manifest.evaluation_end:
        raise ValueError("backtest must end on the final signal session")
    if any(not isinstance(bar, Bar) or not bar.is_final or bar.instrument_id != market.manifest.instrument_id for bar in records):
        raise ValueError("warmup requires verified completed bars")
    if any(a.session_date >= b.session_date for a, b in zip(records, records[1:])):
        raise ValueError("warmup bars must be strictly ordered")
    source = StrategyVersionStore(root / "strategy-versions").read_upload_source(version_id).decode("utf-8")
    state: dict[str, Any] = {}
    for index, bar in enumerate(records[:-1], start=1):
        result = invoke_strategy_worker(sandbox=sandbox, source=source, event_id=f"warmup:{version_id}:{index}",
                                        bar=bar, state=state)
        # Warmup proposals affect only worker state; no outbox is created.
        state = result.state
    return PaperWarmup(checkpoint=state, signal_bar=records[-1], data_manifest_hash=expected_manifest_hash)
