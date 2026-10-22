"""Child process entry point for an independently launched BackBroker replay."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pandas as pd

from src.bt_runtime.report_replay import replay_backtrader_order_log
from src.execution.contracts import Bar, canonical_sha256


def _run(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {"protocol_version", "inputs", "input_sha256"}:
        raise ValueError("invalid replay request shape")
    inputs = request["inputs"]
    if request["protocol_version"] != "python-upload-replay/1" or not isinstance(inputs, dict):
        raise ValueError("unsupported replay protocol")
    if request["input_sha256"] != canonical_sha256(inputs):
        raise ValueError("replay input digest mismatch")
    required = {
        "bars", "bar_records", "decision_log", "initial_cash", "commission_rate",
        "slippage_fraction", "evaluation_start", "data_manifest_hash", "instrument_id",
    }
    if set(inputs) != required or not isinstance(inputs["bars"], list) or not isinstance(inputs["bar_records"], list):
        raise ValueError("invalid replay inputs")
    rows = inputs["bars"]
    frame_rows = []
    timestamps = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"timestamp", "open", "high", "low", "close", "volume"}:
            raise ValueError("invalid replay bar row")
        timestamps.append(pd.Timestamp(row["timestamp"]))
        frame_rows.append({field: row[field] for field in ("open", "high", "low", "close", "volume")})
    frame = pd.DataFrame(frame_rows, index=pd.DatetimeIndex(timestamps, name="date"))
    records = tuple(Bar.model_validate(item) for item in inputs["bar_records"])
    replay = replay_backtrader_order_log(
        frame,
        records,
        inputs["decision_log"],
        initial_cash=inputs["initial_cash"],
        commission_rate=inputs["commission_rate"],
        slippage_fraction=inputs["slippage_fraction"],
        data_manifest_hash=inputs["data_manifest_hash"],
        instrument_id=inputs["instrument_id"],
        evaluation_start=inputs["evaluation_start"],
    )
    return {
        "protocol_version": "python-upload-replay/1",
        "input_sha256": request["input_sha256"],
        "replay": replay,
        "replay_sha256": canonical_sha256(replay),
        "worker_pid": os.getpid(),
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        response = _run(request)
        sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except Exception as exc:
        # Keep diagnostics out of the protocol stream; the parent fails closed.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
