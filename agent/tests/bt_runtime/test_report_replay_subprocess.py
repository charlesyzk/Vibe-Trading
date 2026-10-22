from __future__ import annotations

import json
import os
import sys
from datetime import timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from src.bt_runtime import report_replay
from src.bt_runtime.report_replay import ReportReplayMismatch, replay_backtrader_order_log_subprocess
from src.execution.contracts import Bar, canonical_sha256


def _inputs():
    index = pd.date_range("2024-01-02", periods=3, freq="B", tz="UTC", name="date")
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.5, 11.0],
            "high": [11.0, 11.5, 12.0],
            "low": [9.0, 9.5, 10.0],
            "close": [10.2, 10.7, 11.2],
            "volume": [1000.0, 1100.0, 1200.0],
        },
        index=index,
    )
    records = []
    for stamp, row in bars.iterrows():
        available = stamp.to_pydatetime().astimezone(timezone.utc)
        records.append(Bar(
            instrument_id="TEST",
            session_date=available.date(),
            start_at=available - timedelta(minutes=1),
            end_at=available,
            available_at=available,
            timezone="UTC",
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
            currency="USD",
            is_final=True,
            source="fixture",
            adjustment="unadjusted",
        ))
    return bars, tuple(records)


def test_replay_runs_in_child_process_with_hash_bound_real_backbroker_result():
    bars, records = _inputs()
    replay = replay_backtrader_order_log_subprocess(
        bars, records, (), initial_cash=1000, commission_rate=0.001,
        data_manifest_hash="a" * 64, instrument_id="TEST",
    )

    assert replay["engine"].startswith("Backtrader ")
    assert replay["feed_input_sha256"] == canonical_sha256({
        "bars": [
            {
                "timestamp": pd.Timestamp(timestamp).to_pydatetime(),
                "open": float(row.open), "high": float(row.high), "low": float(row.low),
                "close": float(row.close), "volume": float(row.volume),
            }
            for timestamp, row in bars.iterrows()
        ],
        "bar_records": [bar.model_dump(mode="json") for bar in records],
    })
    assert replay["initial_cash"] == 1000
    assert replay["orders"] == []


@pytest.mark.parametrize("mutation", ["stale_input", "forged_replay_digest", "same_process"])
def test_replay_rejects_stale_or_forged_child_response(monkeypatch, mutation):
    bars, records = _inputs()
    fake_replay = {"engine": "forged"}
    def fake_run(request_bytes, **kwargs):
        request = json.loads(request_bytes)
        response = {
            "protocol_version": "python-upload-replay/1",
            "input_sha256": "0" * 64 if mutation == "stale_input" else request["input_sha256"],
            "replay": fake_replay,
            "replay_sha256": "0" * 64 if mutation == "forged_replay_digest" else canonical_sha256(fake_replay),
            "worker_pid": os.getpid() if mutation == "same_process" else os.getpid() + 1,
        }
        return 0, json.dumps(response).encode("utf-8"), b""

    monkeypatch.setattr(report_replay, "_run_replay_worker", fake_run)

    with pytest.raises(ReportReplayMismatch):
        replay_backtrader_order_log_subprocess(
            bars, records, (), initial_cash=1000, commission_rate=0.001,
            data_manifest_hash="a" * 64, instrument_id="TEST",
        )


@pytest.mark.parametrize(("stream", "size"), [("stdout", 20_000_000), ("stderr", 100_000)])
def test_replay_child_output_is_capped_and_child_is_terminated(tmp_path, stream, size):
    command = [
        sys.executable,
        "-c",
        f"import sys; sys.{stream}.write('x' * {size}); sys.{stream}.flush()",
    ]
    with pytest.raises(ReportReplayMismatch, match=f"{stream} exceeded its output limit"):
        report_replay._run_replay_worker(
            b"", timeout_seconds=10, cwd=tmp_path, command=command,
        )


def test_replay_rejects_oversized_decision_reason_before_child_launch(monkeypatch):
    bars, records = _inputs()
    invoked = False

    def forbidden_child(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("oversized reason must fail before subprocess launch")

    monkeypatch.setattr(report_replay, "_run_replay_worker", forbidden_child)
    with pytest.raises(ReportReplayMismatch, match="decision reason exceeds"):
        replay_backtrader_order_log_subprocess(
            bars, records, [{"seq": 1, "session_date": "2024-01-02", "side": "BUY",
                             "requested_qty": 1, "reason": "x" * 1_025}],
            initial_cash=1000, commission_rate=0.001,
            data_manifest_hash="a" * 64, instrument_id="TEST",
        )
    assert invoked is False
