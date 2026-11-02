"""Opt-in real WASI probes; no broker or uploaded user strategy is involved."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest


ROOT = os.environ.get("VIBE_WASI_TEST_ROOT")
pytestmark = pytest.mark.skipif(not ROOT or importlib.util.find_spec("wasmtime") is None,
                                reason="set VIBE_WASI_TEST_ROOT to a verified WASI Python runtime")


def invoke(source, *, env=None):
    root = Path(ROOT)
    host = Path(__file__).resolve().parents[2] / "src" / "execution" / "wasi_worker_host.py"
    request = {"op": "strategy_event", "protocol_version": 1, "request_id": "a" * 32, "event_id": "fixture",
               "source": source, "event": {"bar": {"close": "12.3"}}, "state": {}}
    return subprocess.run([sys.executable, "-I", str(host), str(root / "bin" / "python-3.12.0.wasm"),
                           str(root / "usr" / "local" / "lib"), str(512 * 1024 * 1024)], input=json.dumps(request) + "\n",
                          text=True, capture_output=True, timeout=30, env=env)


def test_real_wasi_worker_runs_event_protocol():
    result = invoke("def on_event(event, state):\n return {'state': {'seen': event['bar']['close']}, 'order_proposals': []}\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == {"seen": "12.3"}


def test_real_wasi_worker_supports_allowed_standard_library():
    source = '''import collections
import decimal
import functools
import itertools
import math
import statistics
def on_event(event, state):
    values = list(itertools.islice(itertools.count(1), 3))
    total = functools.reduce(lambda a, b: a + b, values)
    return {"state": {"mean": statistics.mean(values), "sqrt": math.sqrt(9),
                       "decimal": str(decimal.Decimal("0.1") + decimal.Decimal("0.2")),
                       "count": collections.Counter(values)[1], "total": total}, "order_proposals": []}
'''
    result = invoke(source)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == {"mean": 2, "sqrt": 3.0, "decimal": "0.3", "count": 1, "total": 6}


def test_real_wasi_worker_cannot_access_host_environment_files_or_network(tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("private", encoding="utf-8")
    source = f'''import os
def on_event(event, state):
    result = {{"env": os.environ.get("VIBE_WASI_SENTINEL")}}
    try:
        open({str(secret)!r}).read()
        result["host_file"] = True
    except Exception:
        result["host_file"] = False
    try:
        open("/usr/local/lib/wasi-write-test", "w").write("x")
        result["stdlib_write"] = True
    except Exception:
        result["stdlib_write"] = False
    try:
        import socket
        socket.socket()
        result["socket"] = True
    except Exception:
        result["socket"] = False
    return {{"state": result, "order_proposals": []}}
'''
    result = invoke(source, env={**os.environ, "VIBE_WASI_SENTINEL": "private"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == {"env": None, "host_file": False, "stdlib_write": False, "socket": False}
    assert secret.read_text(encoding="utf-8") == "private"


def test_wasi_sandbox_executes_standard_worker_protocol_and_checks_pins(tmp_path):
    from src.execution.event_worker import invoke_strategy_worker
    from src.execution.python_sandbox import SandboxUnavailable
    from src.execution.wasi_python_sandbox import WasiPythonSandbox, WASI_WORKER_ID
    from tests.execution.test_outbox_dispatch import _bar

    root = Path(ROOT)
    runtime = tmp_path / "runtime"
    (runtime / "stdlib").mkdir(parents=True)
    shutil.copyfile(root / "bin" / "python-3.12.0.wasm", runtime / "python.wasm")
    shutil.copyfile(root / "usr" / "local" / "lib" / "python312.zip", runtime / "stdlib" / "python312.zip")
    sandbox = WasiPythonSandbox(runtime)
    result = invoke_strategy_worker(sandbox=sandbox, source="def on_event(event, state):\n return {'state': {'n': state.get('n', 0) + 1}, 'order_proposals': []}\n",
                                    event_id="fixture", bar=_bar(), state={})
    assert sandbox.image == WASI_WORKER_ID and result.state == {"n": 1}
    (runtime / "stdlib" / "unexpected.txt").write_text("unexpected")
    with pytest.raises(SandboxUnavailable, match="standard library"):
        sandbox.run_json({})
    (runtime / "stdlib" / "unexpected.txt").unlink()
    with (runtime / "python.wasm").open("ab") as stream:
        stream.write(b"altered")
    with pytest.raises(SandboxUnavailable, match="pinned digest"):
        sandbox.run_json({})


def test_wasi_guest_infinite_loop_is_terminated(tmp_path):
    from src.execution.python_sandbox import SandboxLimits, SandboxTimeout
    from src.execution.wasi_python_sandbox import WasiPythonSandbox

    root = Path(ROOT)
    runtime = tmp_path / "runtime"
    (runtime / "stdlib").mkdir(parents=True)
    shutil.copyfile(root / "bin" / "python-3.12.0.wasm", runtime / "python.wasm")
    shutil.copyfile(root / "usr" / "local" / "lib" / "python312.zip", runtime / "stdlib" / "python312.zip")
    sandbox = WasiPythonSandbox(runtime, limits=SandboxLimits(timeout_seconds=3))
    request = {"op": "strategy_event", "protocol_version": 1, "request_id": "a" * 32, "event_id": "fixture",
               "source": "def on_event(event, state):\n while True: pass\n", "event": {"bar": {}}, "state": {}}
    with pytest.raises(SandboxTimeout):
        sandbox.run_json(request)


def test_wasi_strategy_runs_backtest_and_paper_warmup(tmp_path, monkeypatch):
    from src.execution.paper_strategy_warmup import build_paper_warmup
    from src.execution.wasi_python_sandbox import WasiPythonSandbox
    from tests.bt_runtime import test_python_upload_service as service_tests

    root = Path(ROOT)
    runtime = tmp_path / "wasi-runtime"
    (runtime / "stdlib").mkdir(parents=True)
    shutil.copyfile(root / "bin" / "python-3.12.0.wasm", runtime / "python.wasm")
    shutil.copyfile(root / "usr" / "local" / "lib" / "python312.zip", runtime / "stdlib" / "python312.zip")
    sandbox = WasiPythonSandbox(runtime)
    monkeypatch.setattr(service_tests, "NoOrderSandbox", lambda: sandbox)
    service_tests.test_service_persists_exact_imported_data_version_manifest_and_backtrader_report(tmp_path)
    report = json.loads((tmp_path / "run" / "artifacts" / "execution_report.json").read_text())
    assert report["execution_integrity"]["status"] == "verified"
    shutil.copytree(tmp_path / "versions", tmp_path / "strategy-versions")
    (tmp_path / "runs").mkdir()
    shutil.move(tmp_path / "run", tmp_path / "runs" / "run")
    warmup = build_paper_warmup(runtime_root=tmp_path, run_id="run", version_id=report["strategy_version_hash"],
                                expected_manifest_hash=report["data_manifest_hash"], sandbox=sandbox)
    assert warmup.checkpoint == {} and warmup.signal_bar.session_date.isoformat() == "2025-02-07"


def test_wasi_worker_runs_upload_preview_and_backtest_api(tmp_path, monkeypatch):
    from datetime import date, datetime, timedelta, timezone
    from src.execution.wasi_python_sandbox import WASI_WORKER_ID
    from tests.test_backtrader_backtest_api import _client

    root = Path(ROOT)
    runtime = tmp_path / "wasi-runtime"
    (runtime / "stdlib").mkdir(parents=True)
    shutil.copyfile(root / "bin" / "python-3.12.0.wasm", runtime / "python.wasm")
    shutil.copyfile(root / "usr" / "local" / "lib" / "python312.zip", runtime / "stdlib" / "python312.zip")
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_WASI_WORKER_ROOT", str(runtime))
    days = [date(2025, 2, day) for day in (3, 4, 5)]
    sessions, lines = [], ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
    for index, day in enumerate(days):
        opened = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30)
        closed = opened.replace(hour=21, minute=0)
        sessions.append({"session_date": day.isoformat(), "open_at": opened.isoformat(), "close_at": closed.isoformat()})
        value = 10 + index
        lines.append(f"{day.isoformat()},{opened.isoformat()},{closed.isoformat()},"
                     f"{(closed + timedelta(minutes=1)).isoformat()},{value},{value + 1},{value - 1},{value}.5,1000")
    payload = {"strategy_id": "wasi_fixture", "instrument_id": "AAPL",
        "strategy_source": "def on_event(event, state):\n return {'state': state, 'order_proposals': []}\n",
        "market_data_csv": "\n".join(lines) + "\n", "expected_sessions": sessions,
        "market_data_metadata": {"provider": "fixture", "instrument_id": "AAPL", "currency": "USD",
            "timezone": "America/New_York", "calendar_id": "XNYS", "calendar_version": "4.13.2",
            "availability_model": "completed", "adjustment_policy": "fixture", "corporate_action_policy": "none",
            "fetched_at": "2025-02-06T00:00:00Z", "warmup_start": days[0].isoformat(),
            "warmup_end": days[0].isoformat(), "evaluation_start": days[1].isoformat(),
            "evaluation_end": days[2].isoformat(), "download_parameters": {"fixture": True}},
        "initial_cash": 1000, "commission_rate": 0.001}
    preview = client.post("/runs/backtests/python-upload/preview", json=payload)
    assert preview.status_code == 200 and preview.json()["worker_image"] == WASI_WORKER_ID, preview.text
    result = client.post("/runs/backtests/python-upload", json={"strategy_id": payload["strategy_id"],
        "instrument_id": "AAPL", "strategy_source": payload["strategy_source"], "initial_cash": 1000,
        "commission_rate": 0.001, "preview_token": preview.json()["preview_token"]})
    assert result.status_code == 200, result.text
    assert result.json()["execution_report"]["execution_integrity"]["status"] == "verified"


def test_wasi_runtime_is_selected_for_local_paper_draft_factory(tmp_path, monkeypatch):
    from src.session.models import AuthMethod
    from tests.execution.test_local_paper_draft_factory import test_local_factory_wires_saved_draft_without_connecting

    root = Path(ROOT)
    runtime = tmp_path / "wasi-runtime"
    (runtime / "stdlib").mkdir(parents=True)
    shutil.copyfile(root / "bin" / "python-3.12.0.wasm", runtime / "python.wasm")
    shutil.copyfile(root / "usr" / "local" / "lib" / "python312.zip", runtime / "stdlib" / "python312.zip")
    monkeypatch.setenv("VIBE_WASI_WORKER_ROOT", str(runtime))
    test_local_factory_wires_saved_draft_without_connecting(tmp_path, monkeypatch, AuthMethod.LOCAL_DESKTOP_PROFILE)
