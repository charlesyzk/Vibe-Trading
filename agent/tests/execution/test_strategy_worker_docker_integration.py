"""Opt-in real-container check for isolated uploaded strategy execution."""

from __future__ import annotations

import os
import json
import pwd
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pytest
import backtrader as bt
import pandas as pd

from src.bt_runtime.strategies import sma
from src.bt_runtime.strategies.python_upload import run_python_upload_backtest
from src.execution.contracts import Bar, Environment, StrategySpec
from src.execution.event_journal import SQLiteEventJournal
from src.execution.python_sandbox import PythonSandbox, SandboxLimits, SandboxTimeout
from src.execution.event_worker import EventWorkerError, PYTHON_WORKER_COMPILER_VERSION, PythonEventCoordinator
from src.execution.versions import StrategyVersionStore


CONTEXT_DEFAULT = "colima-vibe-execution"
WORKER_DIR = Path(__file__).parents[2] / "src" / "execution" / "strategy_worker_image"


def _docker(context: str, *args: str, env: dict[str, str], timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["docker", "--context", context, *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:2000]
        pytest.fail(f"Docker command failed ({result.returncode}): {args!r}: {detail}")
    return result


@pytest.mark.integration
def test_uploaded_code_runs_per_event_inside_network_and_host_file_isolation(tmp_path, monkeypatch):
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)
    security_options = json.loads(
        _docker(context, "info", "--format", "{{json .SecurityOptions}}", env=docker_env).stdout
    )
    seccomp_profiles = [
        part.partition("=")[2]
        for option in security_options
        for part in option.split(",")
        if part.partition("=")[0] == "profile"
        and "name=seccomp" in option
    ]
    assert len(seccomp_profiles) == 1
    assert seccomp_profiles[0] in {"builtin", "default"}

    image_tag = f"vibe-strategy-worker:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    sentinel = tmp_path / "strategy-host-only.txt"
    sentinel.write_text("host-secret", encoding="utf-8")

    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()
        source = f'''\
import socket
def on_event(event, state):
    try:
        with open({str(sentinel)!r}, "r", encoding="utf-8") as f:
            can_read_host_file = f.read() == "host-secret"
    except OSError:
        can_read_host_file = False
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", {port}))
        can_connect_host = True
    except OSError:
        can_connect_host = False
    finally:
        s.close()
    return {{"state": {{"host_file_read": can_read_host_file, "host_connect": can_connect_host,
                     "seen_close": event["bar"]["close"]}}, "order_proposals": []}}
'''
        bar = {
            "instrument_id": "us_equity_demo", "session_date": "2026-10-20",
            "start_at": "2026-10-20T13:30:00Z", "end_at": "2026-10-20T20:00:00Z",
            "available_at": "2026-10-20T20:00:00Z", "timezone": "America/New_York",
            "open": "12", "high": "13", "low": "11", "close": "12.5", "volume": "1000",
            "currency": "USD", "is_final": True, "source": "synthetic_fixture",
            "adjustment": "unadjusted", "quality_flags": [],
        }
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))
        result = sandbox.run_json({
            "op": "strategy_event", "protocol_version": 1, "request_id": "c" * 32,
            "source": source, "event_id": "event-1",
            "event": {"bar": bar}, "state": {},
        })
        try:
            listener.accept()
        except TimeoutError:
            pass
        else:
            pytest.fail("uploaded strategy code reached a host loopback listener")
        assert result.response == {
            "protocol_version": 1, "request_id": "c" * 32,
            "event_id": "event-1", "state": {
                "host_file_read": False, "host_connect": False, "seen_close": "12.5",
            }, "order_proposals": [],
        }
    finally:
        listener.close()
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_python_upload_api_preview_confirm_runs_real_docker_worker_and_backbroker(tmp_path, monkeypatch):
    """The user upload API reaches the pinned worker and real report verifier end to end."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    from fastapi.testclient import TestClient
    import api_server
    from src.bt_runtime.calendars import build_xnys_schedule

    image_tag = f"vibe-strategy-worker-api:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(
            context, "image", "inspect", "--format", "{{.Id}}", image_tag, env=docker_env,
        ).stdout.decode().strip()
        monkeypatch.setenv("VIBE_PYTHON_WORKER_IMAGE", worker_image_id)
        monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path / "runs")

        start, end = date(2025, 2, 3), date(2025, 2, 5)
        schedule = build_xnys_schedule(start, end)
        lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
        for index, session in enumerate(schedule.sessions):
            opened = session.open_at.isoformat()
            closed = session.close_at.isoformat()
            available = (session.close_at + timedelta(minutes=1)).isoformat()
            price = 10 + index
            lines.append(
                f"{session.session_date.isoformat()},{opened},{closed},{available},"
                f"{price},{price + 1},{price - 1},{price}.5,1000"
            )
        raw_csv = "\n".join(lines) + "\n"
        payload = {
            "strategy_id": "api_docker_python_fixture",
            "instrument_id": "AAPL",
            "strategy_source": '''\
def on_event(event, state):
    day = event["bar"]["session_date"]
    proposals = [{"side": "BUY", "integer_qty": 1, "reason": "API Docker integration"}] if day == "2025-02-04" else []
    return {"state": {"last_day": day}, "order_proposals": proposals}
''',
            "market_data_csv": raw_csv,
            "market_data_metadata": {
                "provider": "fixture_vendor", "instrument_id": "AAPL", "currency": "USD",
                "timezone": "America/New_York", "calendar_id": schedule.calendar_id,
                "calendar_version": schedule.calendar_version,
                "availability_model": "completed bar available one minute after close",
                "adjustment_policy": "fixture_unadjusted", "corporate_action_policy": "fixture_none",
                "fetched_at": (schedule.sessions[-1].close_at + timedelta(minutes=1)).isoformat(),
                "warmup_start": start.isoformat(), "warmup_end": start.isoformat(),
                "evaluation_start": schedule.sessions[1].session_date.isoformat(),
                "evaluation_end": end.isoformat(),
                "calendar_schedule_sha256": schedule.sha256,
                "download_parameters": {"fixture": True},
            },
            "expected_sessions": [
                {
                    "session_date": row.session_date.isoformat(),
                    "open_at": row.open_at.isoformat(),
                    "close_at": row.close_at.isoformat(),
                    "early_close": row.early_close,
                }
                for row in schedule.sessions
            ],
            "initial_cash": 1000,
            "commission_rate": 0.001,
            "slippage_fraction": 0.0,
        }

        client = TestClient(api_server.app)
        preview_response = client.post("/runs/backtests/python-upload/preview", json=payload)
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["supported"] is True
        assert preview["worker_image"].startswith("sha256:")

        created = client.post("/runs/backtests/python-upload", json={
            "strategy_id": payload["strategy_id"],
            "instrument_id": payload["instrument_id"],
            "strategy_source": payload["strategy_source"],
            "initial_cash": payload["initial_cash"],
            "commission_rate": payload["commission_rate"],
            "slippage_fraction": payload["slippage_fraction"],
            "preview_token": preview["preview_token"],
        })
        assert created.status_code == 200, created.text
        result = created.json()
        report = result["execution_report"]
        assert report["source_kind"] == "python_upload"
        assert report["execution_backend"] == "backtrader"
        assert report["strategy_version_hash"]
        assert report["evaluation_bar_count"] == 2
        assert report["execution_integrity"]["status"] == "verified"
        assert [(order["side"], order["requested_qty"], order["status"]) for order in report["orders"]] == [
            ("BUY", 1, "Completed"),
        ]
        assert report["fills"][0]["executed_at"] == "2025-02-05"
        assert report["final_position"]["qty"] == 1

        detail = client.get(f"/runs/{result['run_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["execution_report_integrity"] == {"status": "verified"}
        assert detail.json()["execution_report"]["strategy_version_hash"] == report["strategy_version_hash"]
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_python_upload_runs_through_isolated_events_and_native_backbroker(tmp_path, monkeypatch):
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))

        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_python_upload_fixture",
            "source_kind": "python_upload", "source_artifact_id": "docker-fixture",
            "instrument_ids": ["synthetic_equity_demo"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "synthetic_test_calendar", "timezone": "UTC"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        source = b'''\
def on_event(event, state):
    # The worker receives one completed bar, not the Backtrader feed or future rows.
    assert "bars" not in event and "future_bars" not in event and "feed" not in event
    try:
        event["bars"][1]
    except (KeyError, TypeError):
        pass
    else:
        raise AssertionError("worker received an indexable future-bar collection")
    day = event["bar"]["session_date"]
    state = {"events_seen": state.get("events_seen", 0) + 1, "last_day": day}
    proposals = []
    if day == "2024-01-04":
        proposals = [{"side": "BUY", "integer_qty": 2, "reason": "docker integration"}]
    return {"state": state, "order_proposals": proposals}
'''
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        index = pd.date_range("2024-01-01", periods=5, freq="B", tz="UTC", name="date")
        frame = pd.DataFrame({
            "open": [10.0, 10.25, 10.5, 10.75, 11.0],
            "high": [11.0, 11.25, 11.5, 11.75, 12.0],
            "low": [9.0, 9.25, 9.5, 9.75, 10.0],
            "close": [10.1, 10.4, 10.6, 10.9, 11.2],
            "volume": [1000] * len(index),
        }, index=index)
        manifest = sma._build_synthetic_manifest(spec, frame, evaluation_start_index=1)

        report = run_python_upload_backtest(
            spec, frame, manifest, version_id=version.version_id,
            versions=versions, sandbox=sandbox, initial_cash=1000, commission_rate=0.001,
        )

        assert report.strategy_version_hash == version.version_id
        assert report.final_position["qty"] == 2
        assert len(report.orders) == 1
        assert report.orders[0]["requested_qty"] == 2
        assert report.orders[0]["status"] == "Completed"
        assert report.fills == ({
            "order_ref": 1, "side": "BUY", "qty": 2, "price": 11.0,
            "commission": 0.022, "executed_at": "2024-01-05",
        },)
        assert len(report.equity_curve) == len(frame)
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_verified_bar_cursor_journal_and_real_docker_worker_share_one_cerebro_run(tmp_path, monkeypatch):
    """The durable host cursor releases one imported bar per actual worker call."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-cursor:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv
        from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
        from src.execution.contracts import StrategySpec
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.python_sandbox import PythonSandbox
        from src.execution.versions import StrategyVersionStore

        days = [date(2025, 2, day) for day in (3, 4, 5)]
        sessions = tuple(MarketSession(
            session_date=day,
            open_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
            close_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
        ) for day in days)
        lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
        for index, session in enumerate(sessions):
            price = 10 + index
            lines.append(
                f"{session.session_date.isoformat()},{session.open_at.isoformat()},{session.close_at.isoformat()},"
                f"{(session.close_at + timedelta(minutes=1)).isoformat()},{price},{price + 1},{price - 1},{price}.5,1000"
            )
        raw_csv = ("\n".join(lines) + "\n").encode("utf-8")
        metadata = MarketDataMetadata(
            provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
            calendar_id="fixture_calendar", calendar_version="2025.1",
            availability_model="completed bar available; one minute after close",
            adjustment_policy="unadjusted", corporate_action_policy="not_verified",
            fetched_at=datetime(2025, 2, 6, tzinfo=timezone.utc),
            warmup_start=days[0], warmup_end=days[0], evaluation_start=days[1], evaluation_end=days[-1],
            download_parameters={"source": "local integration fixture"},
        )
        imported = import_completed_daily_csv(raw_csv, metadata, sessions)
        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_cursor_fixture", "source_kind": "python_upload",
            "template_id": None, "template_version": None, "source_artifact_id": "docker-cursor-fixture",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        source = b'''\
import sys
def on_event(event, state):
    assert set(event) == {"bar"}
    try:
        event["bar"][1]
    except (KeyError, TypeError):
        pass
    else:
        raise AssertionError("current bar unexpectedly exposes a forward index")
    frame = sys._getframe(1)
    request = frame.f_locals.get("request")
    assert isinstance(request, dict)
    assert set(request["event"]) == {"bar"}
    assert not any(key in request for key in ("bars", "future_bars", "feed", "dataframe", "cursor"))
    return {"state": {"observed": state.get("observed", 0) + 1,
                       "session_date": event["bar"]["session_date"]}, "order_proposals": []}
'''
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        import sqlite3
        from src.bt_runtime.backtest_event_journal import BacktestJournalError, SQLiteBacktestEventJournal
        from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
        from src.execution.event_worker import PythonBacktestEventCoordinator

        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))
        actual_run_json = sandbox.run_json
        worker_calls = 0

        def count_worker_calls(request):
            nonlocal worker_calls
            worker_calls += 1
            return actual_run_json(request)

        sandbox.run_json = count_worker_calls
        actual_process_event = PythonBacktestEventCoordinator.process_event

        def forged_future_cursor(self, seq, *, feed_available_at):
            if seq == 1:
                # Forge both the next sequence and its matching timestamp so
                # only the authoritative durable cursor can reject this.
                return actual_process_event(
                    self, seq + 1, feed_available_at=self._released_bars[seq].available_at,
                )
            return actual_process_event(self, seq, feed_available_at=feed_available_at)

        hostile_run_dir = tmp_path / "docker-hostile-cursor-run"
        monkeypatch.setattr(PythonBacktestEventCoordinator, "process_event", forged_future_cursor)
        with pytest.raises(BacktestJournalError, match="active backtest cursor"):
            run_python_upload_service(
                version_id=version.version_id, versions=versions, market_data=imported, raw_csv=raw_csv,
                sandbox=sandbox, run_dir=hostile_run_dir, initial_cash=1000,
            )
        journal_path = hostile_run_dir / "artifacts" / "backtest_events.sqlite3"
        hostile_journal = SQLiteBacktestEventJournal(journal_path)
        assert hostile_journal.get_status(hostile_run_dir.name)["state"] == "PAUSED"
        assert hostile_journal.get_status(hostile_run_dir.name)["next_seq"] == 1
        assert worker_calls == 0
        with sqlite3.connect(journal_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM backtest_event").fetchone()[0] == 0

        monkeypatch.setattr(PythonBacktestEventCoordinator, "process_event", actual_process_event)
        with pytest.raises(BacktestJournalError, match="paused"):
            run_python_upload_service(
                version_id=version.version_id, versions=versions, market_data=imported, raw_csv=raw_csv,
                sandbox=sandbox, run_dir=hostile_run_dir, initial_cash=1000,
            )
        assert worker_calls == 0

        report = run_python_upload_service(
            version_id=version.version_id, versions=versions, market_data=imported, raw_csv=raw_csv,
            sandbox=sandbox,
            run_dir=tmp_path / "docker-cursor-run", initial_cash=1000,
        )

        assert report["execution_integrity"]["event_journal"] == "durable_backtest_replay"
        assert report["execution_integrity"]["event_count"] == len(sessions)
        assert report["execution_integrity"]["newly_computed_results"] == len(sessions)
        assert len(report["equity_curve"]) == len(sessions)
        import sqlite3
        with sqlite3.connect(tmp_path / "docker-cursor-run" / "artifacts" / "backtest_events.sqlite3") as connection:
            rows = connection.execute(
                "SELECT seq, json_extract(bar_json, '$.session_date') FROM backtest_event ORDER BY seq"
            ).fetchall()
        assert rows == [(1, "2025-02-03"), (2, "2025-02-04"), (3, "2025-02-05")]
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_worker_cannot_introspect_future_bar_canary_from_first_cerebro_event(tmp_path, monkeypatch):
    """A future-only CSV value stays outside every bounded worker introspection surface."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-canary:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv
        from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
        from src.execution.contracts import StrategySpec
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits
        from src.execution.versions import StrategyVersionStore

        days = [date(2025, 3, day) for day in (3, 4, 5)]
        sessions = tuple(MarketSession(
            session_date=day,
            open_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
            close_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
        ) for day in days)
        # This unpredictable numeric marker is placed only in the second CSV
        # row. The uploaded source never receives it as a constant or argument.
        canary = str(uuid.uuid4().int % 800_000_000 + 100_000_000)
        closes = ["10.5", canary, "12.5"]
        lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
        for index, (session, close) in enumerate(zip(sessions, closes)):
            if index == 1:
                price = int(canary.split(".")[0])
                open_value, high_value, low_value = close, price + 2, price - 2
            else:
                price = 10 + index
                open_value, high_value, low_value = price, price + 1, price - 1
            lines.append(
                f"{session.session_date.isoformat()},{session.open_at.isoformat()},{session.close_at.isoformat()},"
                f"{(session.close_at + timedelta(minutes=1)).isoformat()},{open_value},{high_value},{low_value},{close},1000"
            )
        raw_csv = ("\n".join(lines) + "\n").encode("utf-8")
        assert canary.encode("ascii") in raw_csv
        assert canary.encode("ascii") not in lines[1].encode("utf-8")
        metadata = MarketDataMetadata(
            provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
            calendar_id="fixture_calendar", calendar_version="2025.1",
            availability_model="completed bar available; one minute after close",
            adjustment_policy="unadjusted", corporate_action_policy="not_verified",
            fetched_at=datetime(2025, 3, 6, tzinfo=timezone.utc),
            warmup_start=days[0], warmup_end=days[0], evaluation_start=days[1], evaluation_end=days[-1],
            download_parameters={"source": "local integration fixture"},
        )
        imported = import_completed_daily_csv(raw_csv, metadata, sessions)
        assert str(imported.bar_records[1].close) == canary
        assert str(imported.bar_records[0].close) != canary

        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_future_canary", "source_kind": "python_upload",
            "template_id": None, "template_version": None, "source_artifact_id": "docker-future-canary",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        run_dir = tmp_path / "docker-future-canary-run"
        artifact_paths = [
            str(run_dir / "artifacts" / name)
            for name in ("market_data.csv", "verified_bars.json", "backtest_events.sqlite3")
        ]
        source_template = br'''
import gc
import os
import re
import sys
import types

def _probe():
    numeric_tokens = set()
    seen = set()
    budget = [0]
    truncated = [False]
    def walk(value, depth):
        if budget[0] >= 60000:
            truncated[0] = True
            return
        budget[0] += 1
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, (str, int, float)):
                numeric_tokens.update(re.findall(r"(?<![0-9])[0-9]{8,}(?:\.[0-9]+)?(?![0-9])", str(value)[:4096]))
            if len(numeric_tokens) > 5000:
                truncated[0] = True
            return
        if depth >= 5 or id(value) in seen:
            return
        seen.add(id(value))
        try:
            if isinstance(value, dict):
                for key, item in list(value.items())[:100]:
                    walk(key, depth + 1)
                    walk(item, depth + 1)
            elif isinstance(value, (tuple, list, set, frozenset)):
                for item in list(value)[:100]:
                    walk(item, depth + 1)
            elif isinstance(value, types.FrameType):
                walk(value.f_locals, depth + 1)
                walk(value.f_globals, depth + 1)
            elif isinstance(value, types.ModuleType):
                walk(vars(value), depth + 1)
        except BaseException:
            pass

    # Inspect all Python frames and the modules they can reach, then a bounded
    # sample of GC-tracked objects, without names or values supplied by host.
    frames = list(sys._current_frames().values())
    for frame in frames:
        walk(frame, 0)
        while frame.f_back is not None:
            frame = frame.f_back
            walk(frame, 0)
    modules = list(sys.modules.values())
    for module in modules:
        if isinstance(module, types.ModuleType):
            walk(module, 0)
    for obj in gc.get_objects()[:20000]:
        walk(obj, 0)

    proc = []
    for path in ("/proc/self/environ", "/proc/self/cmdline", "/proc/1/cmdline", "/proc/self/maps"):
        try:
            with open(path, "rb") as handle:
                proc.append(path + ":" + handle.read(8192).decode("utf-8", "replace"))
        except OSError:
            proc.append(path + ":<unavailable>")
    for directory in ("/proc/self/fd", "/proc/1/fd"):
        try:
            names = os.listdir(directory)[:64]
        except OSError:
            names = []
        for name in names:
            path = directory + "/" + name
            try:
                proc.append(path + "->" + os.readlink(path))
            except OSError:
                pass
            try:
                if os.path.isfile(path):
                    with open(path, "rb") as handle:
                        proc.append(path + ":" + handle.read(4096).decode("utf-8", "replace"))
            except OSError:
                pass
    for text in proc:
        numeric_tokens.update(re.findall(r"(?<![0-9])[0-9]{8,}(?:\.[0-9]+)?(?![0-9])", text[:8192]))
    return {"numeric_tokens": list(numeric_tokens), "proc": proc, "truncated": truncated[0],
            "objects_scanned": budget[0], "numeric_token_count": len(numeric_tokens)}

def on_event(event, state):
    if event["bar"]["session_date"] == "2025-03-03":
        file_access = []
        for path in ARTIFACT_PATHS:
            try:
                with open(path, "rb") as handle:
                    content = handle.read(2 * 1024 * 1024)
                file_access.append({"path": path, "readable": True, "bytes_read": len(content)})
            except OSError:
                file_access.append({"path": path, "readable": False, "bytes_read": 0})
        state = {"probe": _probe(), "first_event_close": event["bar"]["close"], "file_access": file_access}
    else:
        state = {"completed_event": event["bar"]["session_date"]}
    return {"state": state, "order_proposals": []}
'''
        source = source_template.replace(b"ARTIFACT_PATHS", repr(artifact_paths).encode("utf-8"))
        assert canary.encode("ascii") not in source
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=15, max_request_bytes=512_000))
        actual_run_json = sandbox.run_json
        worker_outputs = []

        def capture_real_worker(request):
            artifact_dir = run_dir / "artifacts"
            assert (artifact_dir / "market_data.csv").read_bytes() == raw_csv
            assert canary.encode("ascii") in (artifact_dir / "verified_bars.json").read_bytes()
            assert (artifact_dir / "backtest_events.sqlite3").is_file()
            result = actual_run_json(request)
            worker_outputs.append(result.response)
            return result

        sandbox.run_json = capture_real_worker
        report = run_python_upload_service(
            version_id=version.version_id, versions=versions, market_data=imported, raw_csv=raw_csv,
            sandbox=sandbox, run_dir=run_dir, initial_cash=1000,
        )

        assert report["execution_backend"] == "backtrader"
        assert report["execution_integrity"]["event_count"] == len(sessions)
        assert len(worker_outputs) == len(sessions)
        first_state = worker_outputs[0]["state"]
        assert first_state["first_event_close"] == "10.5"
        probe = first_state["probe"]
        assert all(not item["readable"] for item in first_state["file_access"]), (
            "uploaded strategy could read a host-side market or journal artifact"
        )
        assert probe["truncated"] is False, (
            f"bounded introspection probe hit its inventory limit: {probe['objects_scanned']} objects"
        )
        assert probe["objects_scanned"] > 0
        assert canary not in probe["numeric_tokens"], "future-only CSV canary surfaced in first-event worker introspection"
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_cerebro_prefix_results_are_invariant_to_future_suffix(tmp_path, monkeypatch):
    """Changing only future imported bars cannot change earlier worker or broker results."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-prefix:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv
        from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
        from src.execution.contracts import StrategySpec
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits
        from src.execution.versions import StrategyVersionStore

        days = [date(2025, 4, day) for day in (1, 2, 3, 4, 7)]
        sessions = tuple(MarketSession(
            session_date=day,
            open_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
            close_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
        ) for day in days)
        prefix = ((10, 10.5), (11, 11.5), (12, 12.5))
        suffix_a = ((13, 13.5), (14, 14.5))
        suffix_b = ((30, 30.5), (40, 40.5))

        def import_prices(suffix):
            closes = (*prefix, *suffix)
            lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
            for session, (open_price, close_price) in zip(sessions, closes):
                lines.append(
                    f"{session.session_date.isoformat()},{session.open_at.isoformat()},{session.close_at.isoformat()},"
                    f"{(session.close_at + timedelta(minutes=1)).isoformat()},{open_price},{close_price + 1},"
                    f"{open_price - 1},{close_price},1000"
                )
            raw_csv = ("\n".join(lines) + "\n").encode("utf-8")
            metadata = MarketDataMetadata(
                provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
                calendar_id="fixture_calendar", calendar_version="2025.1",
                availability_model="completed bar available; one minute after close",
                adjustment_policy="unadjusted", corporate_action_policy="not_verified",
                fetched_at=datetime(2025, 4, 8, tzinfo=timezone.utc),
                warmup_start=days[0], warmup_end=days[0], evaluation_start=days[1], evaluation_end=days[-1],
                download_parameters={"source": "local prefix invariance fixture"},
            )
            return raw_csv, import_completed_daily_csv(raw_csv, metadata, sessions)

        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_prefix_invariance", "source_kind": "python_upload",
            "template_id": None, "template_version": None, "source_artifact_id": "docker-prefix-invariance",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        source = b'''\
def on_event(event, state):
    day = event["bar"]["session_date"]
    seen = state.get("seen", 0) + 1
    proposals = [{"side": "BUY", "integer_qty": 1, "reason": "prefix fixture"}] if day == "2025-04-02" else []
    return {"state": {"seen": seen, "last_day": day}, "order_proposals": proposals}
'''
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))
        raw_a, market_a = import_prices(suffix_a)
        raw_b, market_b = import_prices(suffix_b)
        report_a = run_python_upload_service(
            version_id=version.version_id, versions=versions, market_data=market_a, raw_csv=raw_a,
            sandbox=sandbox, run_dir=tmp_path / "prefix-run-a", initial_cash=1000,
        )
        report_b = run_python_upload_service(
            version_id=version.version_id, versions=versions, market_data=market_b, raw_csv=raw_b,
            sandbox=sandbox, run_dir=tmp_path / "prefix-run-b", initial_cash=1000,
        )

        cutoff = "2025-04-03"
        for report in (report_a, report_b):
            assert report["execution_backend"] == "backtrader"
            assert report["execution_integrity"]["event_count"] == len(sessions)
        assert report_a["decision_log"] == report_b["decision_log"]
        assert [row for row in report_a["orders"] if row["decision_date"] <= cutoff] == [
            row for row in report_b["orders"] if row["decision_date"] <= cutoff
        ]
        assert [row for row in report_a["fills"] if row["executed_at"] <= cutoff] == [
            row for row in report_b["fills"] if row["executed_at"] <= cutoff
        ]
        assert report_a["decision_log"] == ({
            "seq": 2, "session_date": "2025-04-02", "side": "BUY", "requested_qty": 1,
            "reason": "prefix fixture",
        },)
        assert report_a["fills"] == report_b["fills"]
        assert report_a["final_value"] != report_b["final_value"]
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_cerebro_suffix_perturbation_is_invariant_after_reopened_sqlite_prefix(tmp_path, monkeypatch):
    """Two frozen runs resume their shared committed prefix before differing suffixes."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-prefix-restart:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        import json
        import sqlite3

        from src.bt_runtime.backtest_event_journal import (
            BacktestRunMismatch,
            SQLiteBacktestEventJournal,
        )
        from src.bt_runtime.market_data import MarketDataMetadata, MarketSession, import_completed_daily_csv
        from src.bt_runtime.strategies.python_upload_service import run_python_upload_service
        from src.execution.contracts import StrategySpec
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION, PythonBacktestEventCoordinator
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits
        from src.execution.versions import StrategyVersionStore

        days = [date(2025, 4, day) for day in (1, 2, 3, 4, 7)]
        sessions = tuple(MarketSession(
            session_date=day,
            open_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
            close_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
        ) for day in days)
        shared_prefix = ((10, 10.5), (11, 11.5), (12, 12.5))
        suffix_a = ((13, 13.5), (14, 14.5))
        suffix_b = ((30, 30.5), (40, 40.5))

        def import_prices(suffix):
            prices = (*shared_prefix, *suffix)
            lines = ["session_date,start_at,end_at,available_at,open,high,low,close,volume"]
            for session, (open_price, close_price) in zip(sessions, prices):
                lines.append(
                    f"{session.session_date.isoformat()},{session.open_at.isoformat()},{session.close_at.isoformat()},"
                    f"{(session.close_at + timedelta(minutes=1)).isoformat()},{open_price},{close_price + 1},"
                    f"{open_price - 1},{close_price},1000"
                )
            raw_csv = ("\n".join(lines) + "\n").encode("utf-8")
            metadata = MarketDataMetadata(
                provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
                calendar_id="fixture_calendar", calendar_version="2025.1",
                availability_model="completed bar available; one minute after close",
                adjustment_policy="unadjusted", corporate_action_policy="not_verified",
                fetched_at=datetime(2025, 4, 8, tzinfo=timezone.utc),
                warmup_start=days[0], warmup_end=days[0], evaluation_start=days[1], evaluation_end=days[-1],
                download_parameters={"source": "local prefix-restart fixture"},
            )
            return raw_csv, import_completed_daily_csv(raw_csv, metadata, sessions)

        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_prefix_restart", "source_kind": "python_upload",
            "template_id": None, "template_version": None, "source_artifact_id": "docker-prefix-restart",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        source = b'''\
def on_event(event, state):
    day = event["bar"]["session_date"]
    close = float(event["bar"]["close"])
    next_state = {"seen": state.get("seen", 0) + 1, "last_day": day, "last_close": close}
    proposals = [{"side": "BUY", "integer_qty": 1, "reason": "prefix restart fixture"}] if day == "2025-04-02" else []
    return {"state": next_state, "order_proposals": proposals}
'''
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))
        worker_calls: list[str] = []
        actual_run_json = sandbox.run_json

        def count_worker_calls(request):
            worker_calls.append(request["event_id"])
            return actual_run_json(request)

        sandbox.run_json = count_worker_calls
        runs = (
            ("prefix-run-a", suffix_a),
            ("prefix-run-b", suffix_b),
        )
        interrupted_prefixes = {}
        reports = {}
        expected_worker_calls: list[str] = []
        original_process_event = PythonBacktestEventCoordinator.process_event

        class SimulatedRestart(RuntimeError):
            pass

        for run_id, suffix in runs:
            raw_csv, market_data = import_prices(suffix)
            run_dir = tmp_path / run_id

            def crash_after_shared_prefix(self, seq, *, feed_available_at, _run_id=run_id):
                result = original_process_event(self, seq, feed_available_at=feed_available_at)
                if self.run_id == _run_id and seq == len(shared_prefix):
                    raise SimulatedRestart("simulated process stop after shared journal prefix")
                return result

            monkeypatch.setattr(PythonBacktestEventCoordinator, "process_event", crash_after_shared_prefix)
            with pytest.raises(SimulatedRestart, match="shared journal prefix"):
                run_python_upload_service(
                    version_id=version.version_id, versions=versions, market_data=market_data, raw_csv=raw_csv,
                    sandbox=sandbox, run_dir=run_dir, initial_cash=1000,
                )
            monkeypatch.setattr(PythonBacktestEventCoordinator, "process_event", original_process_event)

            journal_path = run_dir / "artifacts" / "backtest_events.sqlite3"
            journal = SQLiteBacktestEventJournal(journal_path)
            status = journal.get_status(run_id)
            assert status["state"] == "RUNNING"
            assert status["next_seq"] == len(shared_prefix) + 1
            expected_worker_calls.extend(f"{run_id}:{seq}" for seq in range(1, len(shared_prefix) + 1))
            assert worker_calls == expected_worker_calls
            with sqlite3.connect(journal_path) as connection:
                run_row = connection.execute(
                    "SELECT checkpoint_json FROM backtest_run WHERE run_id=?", (run_id,),
                ).fetchone()
                prefix_rows = connection.execute(
                    "SELECT seq, bar_json, result_json FROM backtest_event WHERE run_id=? ORDER BY seq",
                    (run_id,),
                ).fetchall()
            assert run_row is not None and len(prefix_rows) == len(shared_prefix)
            checkpoint = json.loads(run_row[0])
            assert checkpoint == {"seen": len(shared_prefix), "last_day": "2025-04-03", "last_close": 12.5}
            interrupted_prefixes[run_id] = {
                "checkpoint": checkpoint,
                "bars": [row[1] for row in prefix_rows],
                "results": [json.loads(row[2]) for row in prefix_rows],
            }

            # A fresh journal/coordinator explicitly replays the last committed
            # prefix event after restart. It must return SQLite's result without
            # invoking the worker again.
            reopened = SQLiteBacktestEventJournal(journal_path)
            coordinator = PythonBacktestEventCoordinator(
                journal=reopened, run_id=run_id,
                source=versions.read_upload_source(version.version_id).decode("utf-8"),
                sandbox=sandbox, released_bars=market_data.bar_records,
            )
            last_prefix = coordinator.process_event(
                len(shared_prefix), feed_available_at=market_data.bar_records[len(shared_prefix) - 1].available_at,
            )
            assert last_prefix.state == checkpoint
            assert coordinator.reused_results == 1
            assert worker_calls == expected_worker_calls

            # Resume replays the whole committed prefix into a fresh Cerebro /
            # BackBroker, then computes only the suffix events.
            report = run_python_upload_service(
                version_id=version.version_id, versions=versions, market_data=market_data, raw_csv=raw_csv,
                sandbox=sandbox, run_dir=run_dir, initial_cash=1000,
            )
            assert report["execution_backend"] == "backtrader"
            assert report["execution_integrity"]["event_journal_state"] == "COMPLETE"
            assert report["execution_integrity"]["reused_committed_results"] == len(shared_prefix)
            assert report["execution_integrity"]["newly_computed_results"] == len(sessions) - len(shared_prefix)
            assert journal.get_status(run_id)["state"] == "COMPLETE"
            expected_worker_calls.extend(
                f"{run_id}:{seq}" for seq in range(len(shared_prefix) + 1, len(sessions) + 1)
            )
            assert worker_calls == expected_worker_calls
            reports[run_id] = report

        prefix_a, prefix_b = interrupted_prefixes["prefix-run-a"], interrupted_prefixes["prefix-run-b"]
        assert prefix_a["bars"] == prefix_b["bars"]
        assert prefix_a["checkpoint"] == prefix_b["checkpoint"]
        for rows_a, rows_b in zip(prefix_a["results"], prefix_b["results"]):
            assert rows_a["state"] == rows_b["state"]
            assert rows_a["order_proposals"] == rows_b["order_proposals"]

        report_a, report_b = reports["prefix-run-a"], reports["prefix-run-b"]
        cutoff = "2025-04-03"
        assert report_a["decision_log"] == report_b["decision_log"]
        assert [row for row in report_a["orders"] if row["decision_date"] <= cutoff] == [
            row for row in report_b["orders"] if row["decision_date"] <= cutoff
        ]
        assert [row for row in report_a["fills"] if row["executed_at"] <= cutoff] == [
            row for row in report_b["fills"] if row["executed_at"] <= cutoff
        ]
        assert report_a["decision_log"] == ({
            "seq": 2, "session_date": "2025-04-02", "side": "BUY", "requested_qty": 1,
            "reason": "prefix restart fixture",
        },)
        assert report_a["fills"] == report_b["fills"]
        assert report_a["final_value"] != report_b["final_value"]

        configs = [json.loads((tmp_path / run_id / "artifacts" / "backtest_run_config.json").read_text())
                   for run_id, _ in runs]
        assert configs[0]["data_manifest_hash"] != configs[1]["data_manifest_hash"]
        assert configs[0]["run_config_hash"] != configs[1]["run_config_hash"]

        # The journal binds the entire frozen dataset/config. Rebinding a
        # completed run to another config must pause and reject the resume.
        mismatch_journal = SQLiteBacktestEventJournal(tmp_path / "mismatch.sqlite3")
        mismatch_journal.bind_run(
            run_id="mismatch", strategy_version_hash=version.version_hash,
            strategy_spec_hash=version.spec_hash, run_config_hash="c" * 64,
        )
        with pytest.raises(BacktestRunMismatch, match="run paused"):
            mismatch_journal.bind_run(
                run_id="mismatch", strategy_version_hash=version.version_hash,
                strategy_spec_hash=version.spec_hash, run_config_hash="d" * 64,
            )
        assert mismatch_journal.get_status("mismatch")["state"] == "PAUSED"
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_worker_reports_bounded_strategy_error_without_message_or_traceback(tmp_path, monkeypatch):
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-error:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits, SandboxStrategyFailure

        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))
        with pytest.raises(SandboxStrategyFailure) as caught:
            sandbox.run_json({
                "op": "strategy_event", "protocol_version": 1, "request_id": "d" * 32,
                "event_id": "safe-error-event", "event": {"bar": {"session_date": "2025-04-01"}},
                "state": {},
                "source": "def on_event(event, state):\n    print('SECRET-MARKER')\n    raise NameError('SECRET-MARKER')\n",
            })
        assert caught.value.category == "strategy_runtime_error"
        assert caught.value.error_type == "NameError"
        assert caught.value.source_line == 3
        assert "SECRET-MARKER" not in str(caught.value)
        assert "Traceback" not in str(caught.value)
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_worker_sma_checkpoint_resumes_from_reopened_backtest_journal(tmp_path, monkeypatch):
    """A restarted coordinator resumes a rolling SMA from committed JSON state."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-restart:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        from src.bt_runtime.backtest_event_journal import SQLiteBacktestEventJournal
        from src.execution.contracts import Bar
        from src.execution.event_worker import PythonBacktestEventCoordinator
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits

        bars = tuple(Bar.model_validate({
            "instrument_id": "SYNTHETIC", "session_date": f"2025-04-0{day}",
            "start_at": f"2025-04-0{day}T14:30:00Z", "end_at": f"2025-04-0{day}T21:00:00Z",
            "available_at": f"2025-04-0{day}T21:01:00Z", "timezone": "UTC",
            "open": str(close), "high": str(close + 1), "low": str(close - 1), "close": str(close),
            "volume": "1000", "currency": "USD", "is_final": True,
            "source": "synthetic_fixture", "adjustment": "unadjusted", "quality_flags": [],
        }) for day, close in ((1, 10), (2, 12)))
        source = '''\
def on_event(event, state):
    closes = list(state.get("closes", []))
    closes.append(float(event["bar"]["close"]))
    closes = closes[-2:]
    sma = sum(closes) / 2 if len(closes) == 2 else None
    decision = "buy" if sma is not None and closes[-1] > sma else "hold"
    state = {"closes": closes, "sma_2": sma, "decision": decision}
    proposals = [{"side": "BUY", "integer_qty": 1, "reason": "close above rolling SMA(2)"}] if decision == "buy" else []
    return {"state": state, "order_proposals": proposals}
'''
        sandbox = PythonSandbox(worker_image_id, context=context,
                                limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000))

        worker_calls = []
        actual_run_json = sandbox.run_json

        def count_worker_calls(request):
            worker_calls.append(request["event_id"])
            return actual_run_json(request)

        sandbox.run_json = count_worker_calls

        # The uninterrupted reference is a separate journal, using the same
        # immutable input and actual Docker worker.
        reference_path = tmp_path / "reference.sqlite3"
        reference_journal = SQLiteBacktestEventJournal(reference_path)
        reference_journal.bind_run(
            run_id="reference", strategy_version_hash="a" * 64,
            strategy_spec_hash="b" * 64, run_config_hash="c" * 64,
        )
        reference = PythonBacktestEventCoordinator(
            journal=reference_journal, run_id="reference", source=source,
            sandbox=sandbox, released_bars=bars,
        )
        reference_event_1 = reference.process_event(1, feed_available_at=bars[0].available_at)
        reference_event_2 = reference.process_event(2, feed_available_at=bars[1].available_at)
        assert reference_event_1.state == {"closes": [10.0], "sma_2": None, "decision": "hold"}
        assert reference_event_2.state == {"closes": [10.0, 12.0], "sma_2": 11.0, "decision": "buy"}
        assert [proposal.model_dump() for proposal in reference_event_2.order_proposals] == [{
            "side": "BUY", "integer_qty": 1, "order_type": "market", "time_in_force": "DAY",
            "reason": "close above rolling SMA(2)",
        }]
        assert worker_calls == ["reference:1", "reference:2"]

        # Stop after event 1, close out all journal connections, then construct
        # both a fresh journal object and coordinator as a process restart would.
        resumed_path = tmp_path / "resumed.sqlite3"
        first_journal = SQLiteBacktestEventJournal(resumed_path)
        first_journal.bind_run(
            run_id="resumed", strategy_version_hash="a" * 64,
            strategy_spec_hash="b" * 64, run_config_hash="c" * 64,
        )
        first_coordinator = PythonBacktestEventCoordinator(
            journal=first_journal, run_id="resumed", source=source,
            sandbox=sandbox, released_bars=bars,
        )
        first_event = first_coordinator.process_event(1, feed_available_at=bars[0].available_at)
        assert first_event.state == reference_event_1.state
        assert first_event.order_proposals == reference_event_1.order_proposals
        assert worker_calls == ["reference:1", "reference:2", "resumed:1"]

        reopened_journal = SQLiteBacktestEventJournal(resumed_path)
        restarted = PythonBacktestEventCoordinator(
            journal=reopened_journal, run_id="resumed", source=source,
            sandbox=sandbox, released_bars=bars,
        )
        resumed_event_2 = restarted.process_event(2, feed_available_at=bars[1].available_at)
        assert resumed_event_2.state == reference_event_2.state
        assert resumed_event_2.order_proposals == reference_event_2.order_proposals
        assert worker_calls == ["reference:1", "reference:2", "resumed:1", "resumed:2"]

        # Explicitly replay event 1 after restart: its committed result is read
        # from SQLite and the actual worker is not invoked again.
        replayed_event_1 = restarted.process_event(1, feed_available_at=bars[0].available_at)
        assert replayed_event_1 == first_event
        assert worker_calls == ["reference:1", "reference:2", "resumed:1", "resumed:2"]
        assert restarted.reused_results == 1
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_worker_replays_committed_execution_prefix_without_journal_or_dispatch_writes(tmp_path, monkeypatch):
    """A fresh isolated worker recomputes PAPER prefix outputs without committing or sending."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-shadow-prefix:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        from src.execution.contracts import Bar, Environment
        from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
        from src.execution.python_sandbox import PythonSandbox, SandboxLimits
        from src.execution.strategy_source_policy import validate_strategy_source

        source = '''\
def on_event(event, state):
    closes = list(state.get("closes", []))
    close = float(event["bar"]["close"])
    closes.append(close)
    closes = closes[-3:]
    seen = int(state.get("seen", 0)) + 1
    proposals = []
    if seen == 2 and closes[-1] > closes[-2]:
        proposals = [{"side": "BUY", "integer_qty": 1, "reason": "rolling close increased"}]
    return {"state": {"seen": seen, "closes": closes}, "order_proposals": proposals}
'''
        validate_strategy_source(source)
        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_execution_shadow_replay",
            "source_kind": "python_upload", "source_artifact_id": "docker-shadow-replay",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source.encode("utf-8"),
        )
        database = tmp_path / "execution-shadow-prefix.sqlite3"
        journal = SQLiteEventJournal(database)
        journal.create_run(
            run_id="execution-shadow", deployment_id="deployment-shadow", account_ref="paper-fixture",
            environment=Environment.PAPER, strategy_version_hash=version.version_hash,
            initial_checkpoint={"seen": 0, "closes": []},
        )
        bars = tuple(Bar.model_validate({
            "instrument_id": "AAPL", "session_date": f"2025-04-0{day}",
            "start_at": f"2025-04-0{day}T13:30:00Z", "end_at": f"2025-04-0{day}T20:00:00Z",
            "available_at": f"2025-04-0{day}T20:01:00Z", "timezone": "America/New_York",
            "open": str(close), "high": str(close + 1), "low": str(close - 1), "close": str(close),
            "volume": "1000", "currency": "USD", "is_final": True,
            "source": "synthetic_fixture", "adjustment": "unadjusted", "quality_flags": [],
        }) for day, close in ((1, 10), (2, 12), (3, 11)))
        sandbox = PythonSandbox(
            worker_image_id, context=context,
            limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000),
        )
        calls: list[str] = []
        run_json = sandbox.run_json

        def count_calls(request):
            calls.append(request["event_id"])
            return run_json(request)

        sandbox.run_json = count_calls
        coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)
        outcomes = [
            coordinator.process_next("execution-shadow", next_bar=bar, event_id=f"shadow-event-{index}")
            for index, bar in enumerate(bars, start=1)
        ]
        assert [outcome.result.state for outcome in outcomes] == [
            {"seen": 1, "closes": [10.0]},
            {"seen": 2, "closes": [10.0, 12.0]},
            {"seen": 3, "closes": [10.0, 12.0, 11.0]},
        ]
        assert len(outcomes[1].result.order_proposals) == 1
        assert journal.get_checkpoint("execution-shadow") == outcomes[-1].result.state
        journal.pause_run("execution-shadow")
        before = journal.read_committed_prefix("execution-shadow")
        with sqlite3.connect(database) as connection:
            before_state = connection.execute(
                "SELECT next_seq, checkpoint_json FROM execution_run WHERE run_id='execution-shadow'"
            ).fetchone()
            before_results = connection.execute(
                "SELECT seq, result_json, result_sha256 FROM event_result "
                "WHERE run_id='execution-shadow' ORDER BY seq"
            ).fetchall()
            before_outbox = connection.execute(
                "SELECT seq, proposal_index, proposal_json, status, intent_id FROM order_outbox "
                "WHERE run_id='execution-shadow' ORDER BY seq, proposal_index"
            ).fetchall()

        reopened = SQLiteEventJournal(database)
        restarted = PythonEventCoordinator(
            journal=reopened, versions=versions,
            sandbox=PythonSandbox(worker_image_id, context=context,
                                  limits=SandboxLimits(timeout_seconds=10, max_request_bytes=512_000)),
        )
        replayed = restarted.shadow_replay_committed_prefix("execution-shadow")

        assert tuple(replayed) == tuple(outcome.result for outcome in outcomes)
        assert calls == [f"shadow-event-{index}" for index in range(1, 4)]
        assert reopened.read_committed_prefix("execution-shadow") == before
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT next_seq, checkpoint_json FROM execution_run WHERE run_id='execution-shadow'"
            ).fetchone() == before_state
            assert connection.execute(
                "SELECT seq, result_json, result_sha256 FROM event_result "
                "WHERE run_id='execution-shadow' ORDER BY seq"
            ).fetchall() == before_results
            assert connection.execute(
                "SELECT seq, proposal_index, proposal_json, status, intent_id FROM order_outbox "
                "WHERE run_id='execution-shadow' ORDER BY seq, proposal_index"
            ).fetchall() == before_outbox
            assert connection.execute(
                "SELECT COUNT(*) FROM order_outbox WHERE status!='PENDING'"
            ).fetchone()[0] == 0
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_container_deadline_kills_worker_after_host_supervisor_is_killed(tmp_path, monkeypatch):
    """A killed Docker CLI must not leave uploaded code or its child running."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-deadline:{uuid.uuid4().hex}"
    worker_image_id: str | None = None
    fixed_container_name = f"vibe-py-worker-{uuid.uuid4().hex}"
    host_supervisor: subprocess.Popen[bytes] | None = None
    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()
        uploaded_source = (
            "import os, signal, time\n"
            "def on_event(event, state):\n"
            "    if os.fork() == 0:\n"
            "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "        open('/tmp/vibe-descendant-ready', 'w').write('ready')\n"
            "        while True: time.sleep(1)\n"
            "    while True: pass\n"
        )
        runner = f'''\
import uuid
from src.execution.python_sandbox import PythonSandbox, SandboxLimits
from src.execution import python_sandbox
python_sandbox.uuid.uuid4 = lambda: uuid.UUID(hex={fixed_container_name.removeprefix("vibe-py-worker-")!r})
sandbox = PythonSandbox({worker_image_id!r}, context={context!r},
                        limits=SandboxLimits(timeout_seconds=2, max_request_bytes=512_000))
sandbox.run_json({{
    "op": "strategy_event", "protocol_version": 1, "request_id": "0123456789abcdef0123456789abcdef",
    "source": {uploaded_source!r},
    "event_id": "event-deadline", "event": {{"bar": {{"close": "1"}}}}, "state": {{}}
}})
'''
        runner_env = dict(docker_env, PYTHONPATH=str(Path(__file__).parents[2]))
        host_supervisor = subprocess.Popen(
            [sys.executable, "-c", runner],
            cwd=Path(__file__).parents[2],
            env=runner_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait until uploaded code has started the child and installed its
        # SIGTERM ignore handler before killing the host supervisor. This
        # distinguishes descendant cleanup from a parent-only timeout test.
        deadline = time.monotonic() + 10
        observed_running = False
        observed_descendant = False
        while time.monotonic() < deadline and host_supervisor.poll() is None:
            listed = subprocess.run(
                ["docker", "--context", context, "ps", "--format", "{{.Names}}",
                 "--filter", f"name={fixed_container_name}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=docker_env, timeout=5, check=False,
            )
            if listed.returncode != 0:
                pytest.fail(f"Docker worker inspection failed: {listed.stderr.decode(errors='replace')[:1000]}")
            if fixed_container_name in listed.stdout.decode().splitlines():
                observed_running = True
                ready = subprocess.run(
                    ["docker", "--context", context, "exec", fixed_container_name,
                     "python", "-c", "from pathlib import Path; raise SystemExit(0 if Path('/tmp/vibe-descendant-ready').exists() else 1)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=docker_env, timeout=5, check=False,
                )
                if ready.returncode == 0:
                    observed_descendant = True
                    break
            time.sleep(0.05)
        assert observed_running, "deadline probe worker never became running"
        assert observed_descendant, "uploaded strategy did not start its TERM-ignoring child before supervisor death"
        host_supervisor.kill()
        host_supervisor.wait(timeout=5)

        # The uploaded event started a child that ignores TERM before the
        # parent enters its busy loop. The image-level timeout is shorter than
        # this wait; PID 1/container teardown must remove both even though the
        # host supervisor and its Docker CLI were killed.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            listed = subprocess.run(
                ["docker", "--context", context, "ps", "-a", "--format", "{{.Names}}",
                 "--filter", f"name={fixed_container_name}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=docker_env, timeout=5, check=False,
            )
            if listed.returncode != 0:
                pytest.fail(f"Docker worker inspection failed: {listed.stderr.decode(errors='replace')[:1000]}")
            if fixed_container_name not in listed.stdout.decode().splitlines():
                break
            time.sleep(0.1)
        else:
            pytest.fail("container-level deadline did not stop and auto-remove the worker after host supervisor death")
    finally:
        if host_supervisor is not None and host_supervisor.poll() is None:
            host_supervisor.kill()
            host_supervisor.wait(timeout=5)
        # Idempotent best-effort cleanup if an assertion fails before deadline.
        subprocess.run(
            ["docker", "--context", context, "rm", "--force", "--volumes", fixed_container_name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=docker_env, timeout=10, check=False,
        )
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)


@pytest.mark.integration
def test_real_worker_timeout_preserves_authoritative_released_event_and_removes_container(
    tmp_path, monkeypatch,
):
    """A real sandbox deadline leaves the durable event retryable but immutable."""
    context = os.environ.get("DOCKER_CONTEXT", CONTEXT_DEFAULT)
    actual_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    docker_config = Path(os.environ.get("DOCKER_CONFIG", actual_home / ".docker"))
    if not docker_config.is_dir():
        pytest.fail(f"Docker integration prerequisite missing: {docker_config}")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKER_CONTEXT", context)
    docker_env = dict(os.environ, DOCKER_CONFIG=str(docker_config), DOCKER_CONTEXT=context)
    _docker(context, "info", "--format", "{{.ServerVersion}}", env=docker_env)

    image_tag = f"vibe-strategy-worker-journal-timeout:{uuid.uuid4().hex}"
    worker_image_id: str | None = None

    class TrackingPythonSandbox(PythonSandbox):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.removed_container_names: list[str] = []

        def _remove_container(self, container_name: str) -> None:
            super()._remove_container(container_name)
            self.removed_container_names.append(container_name)

    try:
        _docker(context, "build", "--network=none", "--pull=false", "--file",
                str(WORKER_DIR / "Dockerfile"), "--tag", image_tag, str(WORKER_DIR),
                env=docker_env, timeout=120)
        worker_image_id = _docker(context, "image", "inspect", "--format", "{{.Id}}", image_tag,
                                  env=docker_env).stdout.decode().strip()

        spec = StrategySpec.model_validate({
            "schema_version": "1.0", "strategy_id": "docker_worker_timeout_fixture",
            "source_kind": "python_upload", "source_artifact_id": "docker-timeout-fixture",
            "instrument_ids": ["AAPL"],
            "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                           "calendar_id": "us_equity_rth", "timezone": "America/New_York"},
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                                 "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                                 "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
        })
        source = b"def on_event(event, state):\n    while True: pass\n"
        versions = StrategyVersionStore(tmp_path / "versions")
        version = versions.create_version(
            spec, engine_version=f"backtrader/{bt.__version__}",
            compiler_version=PYTHON_WORKER_COMPILER_VERSION, upload_source=source,
        )
        database = tmp_path / "event-timeout.sqlite"
        journal = SQLiteEventJournal(database)
        journal.create_run(
            run_id="run-timeout", deployment_id="deployment-timeout", account_ref="test-paper-account",
            environment=Environment.PAPER, strategy_version_hash=version.version_hash,
        )
        sandbox = TrackingPythonSandbox(
            worker_image_id, context=context,
            limits=SandboxLimits(timeout_seconds=2, max_request_bytes=512_000),
        )
        coordinator = PythonEventCoordinator(journal=journal, versions=versions, sandbox=sandbox)

        def bar(day: int, close: str) -> dict[str, object]:
            return {
                "instrument_id": "AAPL", "session_date": f"2026-10-{day:02d}",
                "start_at": f"2026-10-{day:02d}T13:30:00Z",
                "end_at": f"2026-10-{day:02d}T20:00:00Z",
                "available_at": f"2026-10-{day:02d}T20:00:00Z", "timezone": "America/New_York",
                "open": close, "high": str(float(close) + 1), "low": str(float(close) - 1),
                "close": close, "volume": "1000", "currency": "USD", "is_final": True,
                "source": "timeout_fixture", "adjustment": "unadjusted", "quality_flags": [],
            }

        authoritative_bar = bar(20, "12.5")
        replacement_bar = bar(21, "99.5")

        def assert_timeout() -> None:
            with pytest.raises(EventWorkerError) as caught:
                coordinator.process_next(
                    "run-timeout", next_bar=authoritative_bar, event_id="authoritative-event",
                )
            cause: BaseException | None = caught.value
            while cause is not None and not isinstance(cause, SandboxTimeout):
                cause = cause.__cause__
            assert isinstance(cause, SandboxTimeout), (
                f"deadline was not classified as SandboxTimeout; error chain: {caught.value!r}"
            )

        assert_timeout()
        first = journal.peek_next_event("run-timeout")
        assert first.event_id == "authoritative-event" and first.seq == 1
        assert set(first.payload) == {"bar"}
        assert Bar.model_validate(first.payload["bar"]) == Bar.model_validate(authoritative_bar)
        with sqlite3.connect(database) as db:
            run = db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='run-timeout'"
            ).fetchone()
            event = db.execute(
                "SELECT released_at, committed_at FROM strategy_event WHERE run_id='run-timeout' AND seq=1"
            ).fetchone()
            result_count = db.execute(
                "SELECT COUNT(*) FROM event_result WHERE run_id='run-timeout'"
            ).fetchone()[0]
            outbox_count = db.execute(
                "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-timeout'"
            ).fetchone()[0]
        assert run == (1,) and event[0] is not None and event[1] is None
        assert result_count == 0 and outbox_count == 0

        with pytest.raises(EventWorkerError) as retry_error:
            coordinator.process_next(
                "run-timeout", next_bar=replacement_bar, event_id="replacement-event",
            )
        cause = retry_error.value
        while cause is not None and not isinstance(cause, SandboxTimeout):
            cause = cause.__cause__
        assert isinstance(cause, SandboxTimeout)
        retried = journal.peek_next_event("run-timeout")
        assert retried == first, "retry replaced the released event with caller-supplied data"
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT next_seq FROM execution_run WHERE run_id='run-timeout'"
            ).fetchone() == (1,)
            assert db.execute(
                "SELECT COUNT(*) FROM event_result WHERE run_id='run-timeout'"
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM order_outbox WHERE run_id='run-timeout'"
            ).fetchone()[0] == 0

        assert len(sandbox.removed_container_names) == 2
        for container_name in sandbox.removed_container_names:
            listed = _docker(
                context, "ps", "-a", "--filter", f"name={container_name}",
                "--format", "{{.Names}}", env=docker_env,
            ).stdout.decode().splitlines()
            assert container_name not in listed, f"timed-out worker container survived: {container_name}"
    finally:
        if worker_image_id:
            _docker(context, "image", "rm", "--force", image_tag, env=docker_env)
