"""Prepare the immutable one-shot AAPL PAPER test strategy version."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import backtrader

from src.bt_runtime.calendars import build_xnys_schedule
from src.config.paths import get_runtime_root
from src.execution.contracts import SourceKind, StrategySpec
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_integration_test_config import PaperIntegrationTestConfig
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


STRATEGY_ID = "paper-aapl-limit-integration-test"
_SOURCE_TEMPLATE = '''def on_event(event, state):
    bar = event["bar"]
    if bar["session_date"] != SIGNAL_SESSION_DATE or state.get("submitted", False):
        return {"state": state, "order_proposals": []}
    return {
        "state": {"submitted": True, "signal_session_date": SIGNAL_SESSION_DATE},
        "order_proposals": [{
            "side": "BUY",
            "integer_qty": 1,
            "order_type": "limit",
            "time_in_force": "DAY",
            "limit_price": "400.00",
            "reason": "one-shot operator-authorized IBKR PAPER integration test",
        }],
    }
'''


def build_paper_integration_test_spec(signal_session_date: date) -> tuple[StrategySpec, bytes]:
    """Build an immutable LMT strategy that emits once on the pinned session."""
    if not isinstance(signal_session_date, date) or isinstance(signal_session_date, datetime):
        # The second check rejects datetime, which is a date subclass.
        raise TypeError("signal_session_date must be a date, not a datetime")
    signal_day = signal_session_date.isoformat()
    source_text = _SOURCE_TEMPLATE.replace("SIGNAL_SESSION_DATE", repr(signal_day))
    source = source_text.encode("utf-8")
    source_hash = hashlib.sha256(source).hexdigest()
    spec = StrategySpec.model_validate({
        "schema_version": "1.0",
        "strategy_id": STRATEGY_ID,
        "source_kind": SourceKind.PYTHON_UPLOAD.value,
        "template_id": None,
        "template_version": None,
        "source_artifact_id": f"upload-{source_hash[:16]}",
        "instrument_ids": ["AAPL"],
        "bar_policy": {
            "timeframe": "1d",
            "session": "regular",
            "completed_only": True,
            "calendar_id": "us_equity_rth",
            "timezone": "America/New_York",
        },
        "parameters": {
            "signal_session_date": signal_day,
            "side": "BUY",
            "quantity": 1,
            "order_type": "limit",
            "limit_price_usd": "400.00",
        },
        "position_policy": {
            "mode": "explicit_quantity",
            "long_only": True,
            "whole_shares": True,
            "allocation_fraction": None,
            "rebalance": None,
            "initial_entry": None,
        },
        "execution_policy": {
            "decision": "completed_bar_available",
            "dispatch": "next_regular_session",
            "order_type": "limit",
            "time_in_force": "DAY",
            "outside_rth": False,
            "gap_action": "reject_and_pause",
            "missed_session_action": "expire_and_pause",
        },
    })
    return spec, source


def prepare_paper_integration_test_strategy(
    config: PaperIntegrationTestConfig,
    *,
    store: StrategyVersionStore | None = None,
) -> StrategyVersionRecord:
    """Freeze/reuse the exact test strategy and return its content-addressed record."""
    if not isinstance(config, PaperIntegrationTestConfig):
        raise TypeError("config must be a validated PaperIntegrationTestConfig")
    if store is None:
        store = StrategyVersionStore(get_runtime_root() / "strategy-versions")
    if not isinstance(store, StrategyVersionStore):
        raise TypeError("store must be a StrategyVersionStore")
    local_start = config.valid_window_utc.start.astimezone(ZoneInfo("America/New_York")).date()
    prior_schedule = build_xnys_schedule(local_start - timedelta(days=10), local_start - timedelta(days=1))
    prior_sessions = [session for session in prior_schedule.sessions if session.close_at < config.valid_window_utc.start]
    if not prior_sessions:
        raise ValueError("integration-test window has no prior completed XNYS session")
    signal_date = prior_sessions[-1].session_date
    spec, source = build_paper_integration_test_spec(signal_date)
    return store.create_version(
        spec,
        upload_source=source,
        engine_version=f"backtrader/{backtrader.__version__}",
        compiler_version=PYTHON_WORKER_COMPILER_VERSION,
    )


__all__ = [
    "STRATEGY_ID",
    "build_paper_integration_test_spec",
    "prepare_paper_integration_test_strategy",
]
