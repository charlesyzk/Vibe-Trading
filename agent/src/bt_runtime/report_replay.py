"""Independent BackBroker replay checks for execution reports."""

from __future__ import annotations

import math
import json
import os
import selectors
import subprocess
import sys
import time
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import backtrader as bt
import pandas as pd

from src.execution.contracts import Bar, canonical_sha256


class ReportReplayMismatch(RuntimeError):
    """The independently replayed order log does not match the host report."""


# Trusted daily templates allow up to ten years of sessions. Python upload has
# a stricter importer limit of its own; keep the shared child protocol large
# enough for the trusted route while remaining bounded by bytes and timeout.
MAX_REPLAY_BARS = 5_000
MAX_REPLAY_REASON_CHARS = 1_024
MAX_REPLAY_REQUEST_BYTES = 8 * 1024 * 1024
MAX_REPLAY_RESPONSE_BYTES = 8 * 1024 * 1024


def _run_replay_worker(
    request_bytes: bytes,
    *,
    timeout_seconds: float,
    cwd: Path,
    command: Sequence[str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run the trusted replay worker with bounded stdin/stdout/stderr pipes."""
    process = subprocess.Popen(
        list(command or (sys.executable, "-m", "src.bt_runtime.report_replay_worker")),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env={
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        bufsize=0,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = (process.stdin, process.stdout, process.stderr)
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    request_offset = 0
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": MAX_REPLAY_RESPONSE_BYTES, "stderr": 64 * 1024}
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("replay worker timed out")
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                kind = key.data
                if kind == "stdin":
                    try:
                        written = os.write(stream.fileno(), request_bytes[request_offset:request_offset + 65_536])
                    except BrokenPipeError:
                        written = 0
                        request_offset = len(request_bytes)
                    request_offset += written
                    if request_offset >= len(request_bytes):
                        selector.unregister(stream)
                        stream.close()
                else:
                    chunk = os.read(stream.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    captured[kind].extend(chunk)
                    if len(captured[kind]) > limits[kind]:
                        raise ReportReplayMismatch(f"replay worker {kind} exceeded its output limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("replay worker timed out")
        return_code = process.wait(timeout=remaining)
        return return_code, bytes(captured["stdout"]), bytes(captured["stderr"])
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()


def replay_backtrader_order_log_subprocess(
    bars: pd.DataFrame,
    bar_records: Sequence[Bar],
    decision_log: Sequence[dict[str, Any]],
    *,
    initial_cash: float,
    commission_rate: float,
    slippage_fraction: float = 0.0,
    data_manifest_hash: str,
    instrument_id: str,
    evaluation_start: date | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Replay serialized, hash-bound inputs in a separately launched process.

    The child receives the complete immutable input envelope over stdin and
    returns a hash-bound replay result over stdout. A missing, stale, malformed,
    or altered response is rejected before the report verifier sees it.
    """
    if len(bars) > MAX_REPLAY_BARS or len(bar_records) > MAX_REPLAY_BARS:
        raise ReportReplayMismatch("separate BackBroker replay exceeds its bar limit")
    if len(decision_log) > MAX_REPLAY_BARS:
        raise ReportReplayMismatch("separate BackBroker replay exceeds its decision limit")
    for row in decision_log:
        reason = row.get("reason") if isinstance(row, dict) else None
        if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REPLAY_REASON_CHARS):
            raise ReportReplayMismatch("decision reason exceeds the replay text limit")
    inputs = {
        "bars": [
            {
                "timestamp": pd.Timestamp(timestamp).isoformat(),
                **{field: float(row[field]) for field in ("open", "high", "low", "close", "volume")},
            }
            for timestamp, row in bars.iterrows()
        ],
        "bar_records": [bar.model_dump(mode="json") for bar in bar_records],
        "decision_log": list(decision_log),
        "initial_cash": float(initial_cash),
        "commission_rate": float(commission_rate),
        "slippage_fraction": float(slippage_fraction),
        "evaluation_start": evaluation_start.isoformat() if evaluation_start is not None else None,
        "data_manifest_hash": data_manifest_hash,
        "instrument_id": instrument_id,
    }
    input_hash = canonical_sha256(inputs)
    request = {
        "protocol_version": "python-upload-replay/1",
        "inputs": inputs,
        "input_sha256": input_hash,
    }
    request_bytes = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(request_bytes) > MAX_REPLAY_REQUEST_BYTES:
        raise ReportReplayMismatch("separate BackBroker replay request exceeds its byte limit")
    agent_root = Path(__file__).resolve().parents[2]
    try:
        return_code, stdout, stderr = _run_replay_worker(
            request_bytes, timeout_seconds=timeout_seconds, cwd=agent_root,
        )
    except (OSError, TimeoutError, subprocess.TimeoutExpired, ReportReplayMismatch) as exc:
        if isinstance(exc, ReportReplayMismatch):
            raise
        raise ReportReplayMismatch("separate BackBroker replay process failed to start or timed out") from exc
    if return_code != 0:
        raise ReportReplayMismatch("separate BackBroker replay process failed")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReportReplayMismatch("separate BackBroker replay returned invalid JSON") from exc
    if not isinstance(response, dict) or set(response) != {
        "protocol_version", "input_sha256", "replay", "replay_sha256", "worker_pid",
    }:
        raise ReportReplayMismatch("separate BackBroker replay response has an invalid shape")
    replay = response["replay"]
    if response["protocol_version"] != "python-upload-replay/1" or response["input_sha256"] != input_hash:
        raise ReportReplayMismatch("separate BackBroker replay response is stale or bound to different inputs")
    if type(response["worker_pid"]) is not int or response["worker_pid"] == os.getpid():
        raise ReportReplayMismatch("BackBroker replay did not run in a separate process")
    if not isinstance(replay, dict) or response["replay_sha256"] != canonical_sha256(replay):
        raise ReportReplayMismatch("separate BackBroker replay response integrity check failed")
    return replay


def replay_backtrader_order_log(
    bars: pd.DataFrame,
    bar_records: Sequence[Bar],
    decision_log: Sequence[dict[str, Any]],
    *,
    initial_cash: float,
    commission_rate: float,
    slippage_fraction: float = 0.0,
    data_manifest_hash: str,
    instrument_id: str,
    evaluation_start: date | str | None = None,
) -> dict[str, Any]:
    """Replay host-captured decisions in a fresh real Cerebro/BackBroker run."""
    records = tuple(bar_records)
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ReportReplayMismatch("replay initial cash must be finite and positive")
    if not math.isfinite(commission_rate) or commission_rate < 0:
        raise ReportReplayMismatch("replay commission rate must be finite and non-negative")
    if not math.isfinite(slippage_fraction) or not 0 <= slippage_fraction <= 0.25:
        raise ReportReplayMismatch("replay slippage fraction must be finite and between 0 and 0.25")
    _validate_replay_inputs(bars, records, instrument_id=instrument_id)
    if isinstance(evaluation_start, str):
        try:
            evaluation_start = date.fromisoformat(evaluation_start)
        except ValueError as exc:
            raise ReportReplayMismatch("evaluation start must be an ISO session date") from exc
    if evaluation_start is not None and not any(record.session_date == evaluation_start for record in records):
        raise ReportReplayMismatch("evaluation start must match a verified session date")
    by_seq: dict[int, dict[str, Any]] = {}
    last_seq = 0
    for row in decision_log:
        if set(row) != {"seq", "session_date", "side", "requested_qty", "reason"}:
            raise ReportReplayMismatch("decision log row has an invalid shape")
        seq = row["seq"]
        if type(seq) is not int or not 1 <= seq <= len(records) or seq <= last_seq:
            raise ReportReplayMismatch("decision log sequence must be valid, unique, and strictly increasing")
        last_seq = seq
        record = records[seq - 1]
        if row["session_date"] != record.session_date.isoformat():
            raise ReportReplayMismatch("decision log date does not match its verified bar sequence")
        if row["side"] not in {"BUY", "SELL"} or type(row["requested_qty"]) is not int or row["requested_qty"] <= 0:
            raise ReportReplayMismatch("decision log side or explicit quantity is invalid")
        by_seq[seq] = dict(row)

    feed_input_hash = canonical_sha256({
        "bars": [
            {
                "timestamp": pd.Timestamp(timestamp).to_pydatetime(),
                **{field: float(row[field]) for field in ("open", "high", "low", "close", "volume")},
            }
            for timestamp, row in bars.iterrows()
        ],
        "bar_records": [bar.model_dump(mode="json") for bar in records],
    })

    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)
    cerebro.broker.setcash(float(initial_cash))
    cerebro.broker.setcommission(commission=float(commission_rate))
    cerebro.broker.set_slippage_perc(
        perc=float(slippage_fraction), slip_open=True, slip_limit=True, slip_match=True, slip_out=False,
    )
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.adddata(bt.feeds.PandasData(dataname=bars, timeframe=bt.TimeFrame.Days))
    cerebro.addstrategy(_make_replay_strategy(records=records, decisions=by_seq))
    strategy = cerebro.run()[0]

    stable_refs = {ref: index for index, ref in enumerate(strategy.order_rows, start=1)}
    orders = tuple(_stable({**row, "order_ref": stable_refs[ref]}) for ref, row in strategy.order_rows.items())
    fills = tuple(_stable({**row, "order_ref": stable_refs[row["order_ref"]]}) for row in strategy.fill_rows)
    position = strategy.getposition(strategy.data)
    drawdown = strategy.analyzers.drawdown.get_analysis()
    returns = strategy.analyzers.returns.get_analysis()
    final_value = float(cerebro.broker.getvalue())
    evaluation_bar_count = (
        sum(record.session_date >= evaluation_start for record in records)
        if evaluation_start is not None else None
    )
    annualized_return = float(returns.get("rnorm", 0.0))
    if evaluation_bar_count is not None:
        periods = max(1, evaluation_bar_count - 1)
        annualized_return = (
            math.expm1(math.log(final_value / float(initial_cash)) / periods * 252.0)
            if final_value > 0 else -1.0
        )
    peak = float(initial_cash)
    max_drawdown_value = 0.0
    for row in strategy.equity_rows:
        peak = max(peak, float(row["equity"]))
        max_drawdown_value = max(max_drawdown_value, peak - float(row["equity"]))
    return _stable({
        "orders": orders,
        "decision_contexts": tuple(strategy.decision_contexts),
        "fills": fills,
        "equity_curve": tuple(strategy.equity_rows),
        "closed_trades": tuple(strategy.closed_trade_rows),
        "final_cash": float(cerebro.broker.getcash()),
        "final_value": final_value,
        "total_fees": sum(fill["commission"] for fill in strategy.fill_rows),
        "final_position": {"qty": int(position.size), "average_price": float(position.price)},
        "evaluation_bar_count": evaluation_bar_count,
        "metrics": {
            "total_return_fraction": final_value / float(initial_cash) - 1.0,
            "max_drawdown_fraction": float(drawdown.get("max", {}).get("drawdown", 0.0)) / 100.0,
            "max_drawdown_value": max_drawdown_value,
            "closed_trade_count": len(strategy.closed_trade_rows),
            "closed_trade_win_count": sum(row["net_pnl"] > 0 for row in strategy.closed_trade_rows),
            "backtrader_annualized_return": annualized_return,
        },
        "initial_cash": float(initial_cash),
        "commission_rate": float(commission_rate),
        "slippage_fraction": float(slippage_fraction),
        "data_manifest_hash": data_manifest_hash,
        "feed_input_sha256": feed_input_hash,
        "decision_log_sha256": canonical_sha256({"decisions": list(decision_log)}),
        "evaluation_start": evaluation_start.isoformat() if evaluation_start is not None else None,
        "engine": f"Backtrader {bt.__version__} / BackBroker",
    })


def verify_backtest_report_replay(
    report: dict[str, Any],
    replay: dict[str, Any],
    *,
    initial_cash: float,
    commission_rate: float,
    slippage_fraction: float = 0.0,
    data_manifest_hash: str,
    strategy_version_hash: str,
    engine: str,
    instrument_id: str,
    strict_order_shape: bool = True,
) -> dict[str, Any]:
    """Fail closed when any execution-bearing report field differs from replay."""
    expected_fields = (
        "fills", "equity_curve", "closed_trades", "final_cash", "final_value",
        "total_fees", "final_position", "metrics",
    )
    if not _matches(
        _execution_order_projection(report.get("orders"), strict=strict_order_shape),
        _execution_order_projection(replay.get("orders"), strict=strict_order_shape),
    ):
        raise ReportReplayMismatch("independent BackBroker replay differs at orders")
    for field in expected_fields:
        if field not in report or field not in replay or not _matches(report[field], replay[field]):
            raise ReportReplayMismatch(f"independent BackBroker replay differs at {field}")
    decisions = report.get("decision_log")
    if not isinstance(decisions, (tuple, list)):
        raise ReportReplayMismatch("host decision log is missing")
    decision_hash = canonical_sha256({"decisions": list(decisions)})
    if replay.get("decision_log_sha256") != decision_hash:
        raise ReportReplayMismatch("decision log changed after independent replay")
    if not _matches(replay.get("initial_cash"), initial_cash) or not _matches(report.get("initial_cash"), initial_cash):
        raise ReportReplayMismatch("initial cash does not match the replay input")
    if not _matches(replay.get("commission_rate"), commission_rate):
        raise ReportReplayMismatch("replay commission rate does not match its configured input")
    if not _matches(replay.get("slippage_fraction"), slippage_fraction):
        raise ReportReplayMismatch("replay slippage fraction does not match its configured input")
    provenance = report.get("provenance")
    cost = provenance.get("cost") if isinstance(provenance, dict) else None
    if not isinstance(cost, dict) or not _matches(cost.get("commission_rate"), str(commission_rate)):
        raise ReportReplayMismatch("report commission declaration does not match replay input")
    if cost.get("slippage_model") != "BackBroker percentage slippage at next-bar market open; capped to bar range":
        raise ReportReplayMismatch("report slippage declaration is not the replayed model")
    if not _matches(cost.get("slippage_fraction"), str(slippage_fraction)):
        raise ReportReplayMismatch("report slippage fraction does not match replay input")
    if report.get("data_manifest_hash") != data_manifest_hash:
        raise ReportReplayMismatch("report data manifest hash does not match the run input")
    manifest_payload = report.get("data_manifest")
    if not isinstance(manifest_payload, dict) or canonical_sha256(manifest_payload) != data_manifest_hash:
        raise ReportReplayMismatch("report data manifest content does not match its hash")
    if report.get("provenance", {}).get("data_manifest_hash") != data_manifest_hash:
        raise ReportReplayMismatch("report provenance data hash does not match the run input")
    if replay.get("evaluation_start") is not None:
        if replay.get("evaluation_start") != manifest_payload.get("evaluation_start"):
            raise ReportReplayMismatch("replay evaluation window does not match the data manifest")
        window = report.get("provenance", {}).get("evaluation_window")
        if not isinstance(window, dict) or any(
            window.get(field) != manifest_payload.get(field)
            for field in ("warmup_start", "warmup_end", "evaluation_start", "evaluation_end")
        ):
            raise ReportReplayMismatch("report evaluation window does not match the data manifest")
        if report.get("evaluation_bar_count") != replay.get("evaluation_bar_count"):
            raise ReportReplayMismatch("report evaluation bar count differs from independent replay")
    if replay.get("data_manifest_hash") != data_manifest_hash:
        raise ReportReplayMismatch("replay data manifest hash does not match the run input")
    if report.get("strategy_version_hash") != strategy_version_hash:
        raise ReportReplayMismatch("report strategy version does not match the frozen run input")
    if report.get("provenance", {}).get("strategy_version_hash") != strategy_version_hash:
        raise ReportReplayMismatch("report provenance strategy version does not match the frozen run input")
    if report.get("engine") != engine or replay.get("engine") != engine:
        raise ReportReplayMismatch("report engine identity does not match the replay engine")
    if report.get("instrument_id") != instrument_id or manifest_payload.get("instrument_id") != instrument_id:
        raise ReportReplayMismatch("report instrument does not match the replay input")
    checked_fields = ["orders", *expected_fields]
    if replay.get("evaluation_start") is not None:
        checked_fields.extend(("evaluation_window", "evaluation_bar_count"))
    return {
        "status": "verified",
        "engine": "second Backtrader Cerebro / BackBroker in separate local process",
        "decision_log_sha256": decision_hash,
        "data_manifest_hash": data_manifest_hash,
        "feed_input_sha256": replay["feed_input_sha256"],
        "initial_cash": float(initial_cash),
        "commission_rate": float(commission_rate),
        "slippage_fraction": float(slippage_fraction),
        "checked_fields": checked_fields,
    }


def verify_trusted_template_sizing_metadata(
    report: dict[str, Any],
    replay: dict[str, Any],
    *,
    allocation_fraction: Decimal,
    strategy_budget: Decimal,
    fee_reserve: Decimal,
    price_buffer_fraction: Decimal,
) -> None:
    """Independently recompute the sizing claims attached to trusted NL2 orders.

    The separate BackBroker replay supplies pre-decision account state and
    current-bar close from its own Cerebro run. The expected sizing policy is
    supplied by the frozen StrategySpec and run configuration, not report
    claims.
    """
    from src.execution.position_sizing import size_target_fraction

    orders = report.get("orders")
    decisions = report.get("decision_log")
    contexts = replay.get("decision_contexts")
    if not isinstance(orders, (tuple, list)) or not isinstance(decisions, (tuple, list)):
        raise ReportReplayMismatch("trusted sizing verification requires order and decision rows")
    if not isinstance(contexts, (tuple, list)) or len(orders) != len(decisions) or len(orders) != len(contexts):
        raise ReportReplayMismatch("trusted sizing decision contexts do not match report orders")

    sizing_provenance = report.get("provenance", {}).get("sizing")
    expected_sizing_provenance = {
        "strategy_budget": str(strategy_budget),
        "fee_reserve": str(fee_reserve),
        "price_buffer_fraction": str(price_buffer_fraction),
    }
    if sizing_provenance != expected_sizing_provenance:
        raise ReportReplayMismatch("trusted sizing provenance does not match frozen run configuration")
    if not _matches(report.get("allocation_fraction"), float(allocation_fraction)):
        raise ReportReplayMismatch("trusted sizing allocation does not match the frozen StrategySpec")

    for index, (order, decision, context) in enumerate(zip(orders, decisions, contexts, strict=True)):
        if not all(isinstance(row, dict) for row in (order, decision, context)):
            raise ReportReplayMismatch(f"trusted sizing row {index} has an invalid shape")
        if set(context) != {"seq", "session_date", "cash", "equity", "position_qty", "reference_price"}:
            raise ReportReplayMismatch(f"trusted sizing decision context {index} has an invalid shape")
        if (
            context["seq"] != decision.get("seq")
            or context["session_date"] != decision.get("session_date")
            or order.get("decision_date") != context["session_date"]
            or order.get("side") != decision.get("side")
        ):
            raise ReportReplayMismatch(f"trusted sizing order {index} does not match its replay decision context")
        decimal_values = {}
        for field in ("cash", "equity", "reference_price"):
            value = context[field]
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ReportReplayMismatch(f"trusted sizing decision context {index} contains invalid values")
            try:
                decimal_values[field] = Decimal(str(value))
            except Exception as exc:
                raise ReportReplayMismatch(
                    f"trusted sizing decision context {index} contains invalid values"
                ) from exc
            if not decimal_values[field].is_finite():
                raise ReportReplayMismatch(f"trusted sizing decision context {index} contains invalid values")
        cash, equity, reference_price = (
            decimal_values["cash"], decimal_values["equity"], decimal_values["reference_price"],
        )
        position_qty = context["position_qty"]
        if type(position_qty) is not int or position_qty < 0:
            raise ReportReplayMismatch(f"trusted sizing decision context {index} contains invalid values")
        side = order.get("side")
        if side == "BUY":
            if "target_shares" not in order:
                raise ReportReplayMismatch(f"trusted sizing order {index} is missing target_shares")
            expected = size_target_fraction(
                entering_target=True,
                allocation_fraction=allocation_fraction,
                account_equity=equity,
                strategy_budget=strategy_budget,
                cash_available=cash,
                reference_price=reference_price,
                fee_reserve=fee_reserve,
                price_buffer_fraction=price_buffer_fraction,
                held_qty=position_qty,
            )
            expected_reason = expected.reason.value
        elif side == "SELL":
            expected = size_target_fraction(
                entering_target=False,
                allocation_fraction=None,
                account_equity=None,
                strategy_budget=None,
                cash_available=None,
                reference_price=reference_price,
                fee_reserve=None,
                price_buffer_fraction=price_buffer_fraction,
                held_qty=position_qty,
                sellable_qty=position_qty,
            )
            expected_reason = None
        else:
            raise ReportReplayMismatch(f"trusted sizing order {index} has an invalid side")
        expected_values = {
            "requested_qty": expected.accepted_qty,
            "sizing_requested_qty": expected.requested_qty,
            "accepted_qty": expected.accepted_qty,
        }
        if expected.target_shares is not None and "target_shares" in order:
            expected_values["target_shares"] = expected.target_shares
        for field, expected_value in expected_values.items():
            if type(order.get(field)) is not int or order[field] != expected_value:
                raise ReportReplayMismatch(f"trusted sizing order {index} differs at {field}")
        sizing_reason = order.get("sizing_reason")
        if side == "BUY" and sizing_reason != expected_reason:
            raise ReportReplayMismatch(f"trusted sizing order {index} differs at sizing_reason")
        if side == "SELL" and sizing_reason not in {"accepted", "exit_position"}:
            raise ReportReplayMismatch(f"trusted exit order {index} has an invalid sizing_reason")
        if "target_shares" in order:
            if type(order["target_shares"]) is not int or order["target_shares"] != (expected.target_shares or 0):
                raise ReportReplayMismatch(f"trusted sizing order {index} differs at target_shares")


# Compatibility for the existing Python-upload report pipeline.
verify_python_report_replay = verify_backtest_report_replay


def _validate_replay_inputs(bars: pd.DataFrame, records: tuple[Bar, ...], *, instrument_id: str) -> None:
    fields = ("open", "high", "low", "close", "volume")
    if not isinstance(bars, pd.DataFrame) or not len(bars) or len(records) != len(bars):
        raise ReportReplayMismatch("replay bars do not match the verified Bar record count")
    if any(field not in bars.columns for field in fields):
        raise ReportReplayMismatch("replay feed is missing a required OHLCV column")
    previous_available_at = None
    for index, (bar, (timestamp, row)) in enumerate(zip(records, bars.iterrows(), strict=True)):
        if not isinstance(bar, Bar) or not bar.is_final:
            raise ReportReplayMismatch("replay records must be validated completed Bar values")
        try:
            stamp = pd.Timestamp(timestamp)
            if stamp.tzinfo is None:
                raise ValueError("naive index")
            available_at = stamp.to_pydatetime().astimezone(timezone.utc)
            local_date = available_at.astimezone(ZoneInfo(bar.timezone)).date()
        except Exception as exc:
            raise ReportReplayMismatch(f"replay feed timestamp/timezone is invalid at index {index}") from exc
        if bar.available_at != available_at or bar.session_date != local_date:
            raise ReportReplayMismatch(f"replay Bar time does not match feed at index {index}")
        if bar.instrument_id != instrument_id:
            raise ReportReplayMismatch(f"replay Bar instrument does not match the frozen strategy at index {index}")
        if previous_available_at is not None and available_at <= previous_available_at:
            raise ReportReplayMismatch("replay bars must be strictly increasing by availability time")
        previous_available_at = available_at
        for field in fields:
            if not math.isfinite(float(row[field])) or float(getattr(bar, field)) != float(row[field]):
                raise ReportReplayMismatch(f"replay Bar {field} does not match feed at index {index}")


def _make_replay_strategy(*, records: tuple[Bar, ...], decisions: dict[int, dict[str, Any]]):
    class OrderLogReplay(bt.Strategy):
        def __init__(self):
            self.seq = 0
            self.pending_order = None
            self.order_rows: dict[int, dict[str, Any]] = {}
            self.fill_rows: list[dict[str, Any]] = []
            self.equity_rows: list[dict[str, Any]] = []
            self.closed_trade_rows: list[dict[str, Any]] = []
            self.reported_exec_bits: dict[int, int] = {}
            self.decision_contexts: list[dict[str, Any]] = []

        def prenext(self):
            self._advance()

        def next(self):
            self._advance()

        def _advance(self):
            if self.seq >= len(records):
                raise ReportReplayMismatch("Backtrader replay emitted more bars than the verified input")
            record = records[self.seq]
            self.equity_rows.append({
                "bar": self.data.datetime.datetime(0).isoformat(),
                "cash": float(self.broker.getcash()),
                "equity": float(self.broker.getvalue()),
                "position_qty": int(self.position.size),
            })
            self.seq += 1
            decision = decisions.get(self.seq)
            if decision is None:
                return
            if self.pending_order is not None:
                raise ReportReplayMismatch("replay decision arrived while a previous order was active")
            self.decision_contexts.append({
                "seq": self.seq,
                "session_date": record.session_date.isoformat(),
                "cash": str(self.broker.getcash()),
                "equity": str(self.broker.getvalue()),
                "position_qty": int(self.position.size),
                "reference_price": str(self.data.close[0]),
            })
            quantity = decision["requested_qty"]
            if decision["side"] == "SELL" and quantity > int(self.position.size):
                raise ReportReplayMismatch("replayed sell decision exceeds the long position")
            order = self.buy(size=quantity) if decision["side"] == "BUY" else self.sell(size=quantity)
            self.pending_order = order
            self.order_rows[order.ref] = {
                "order_ref": order.ref,
                "side": decision["side"],
                "requested_qty": quantity,
                "reason": decision["reason"],
                "status": "Created",
                "status_history": ["Created"],
                "decision_date": record.session_date.isoformat(),
                "filled_qty": 0,
                "remaining_qty": quantity,
            }

        def notify_order(self, order):
            row = self.order_rows.setdefault(order.ref, {
                "order_ref": order.ref,
                "side": "BUY" if order.isbuy() else "SELL",
                "requested_qty": abs(int(order.created.size)),
                "status_history": [],
            })
            status = order.getstatusname()
            if not row["status_history"] or row["status_history"][-1] != status:
                row["status_history"].append(status)
            row["status"] = status
            row["filled_qty"] = abs(int(order.executed.size))
            row["remaining_qty"] = abs(int(order.executed.remsize))
            bits = order.executed.exbits
            offset = self.reported_exec_bits.get(order.ref, 0)
            for bit_index in range(offset, len(bits)):
                bit = bits[bit_index]
                self.fill_rows.append({
                    "order_ref": order.ref,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "qty": abs(int(bit.size)),
                    "price": float(bit.price),
                    "commission": float(bit.comm),
                    "executed_at": bt.num2date(bit.dt).date().isoformat(),
                })
            self.reported_exec_bits[order.ref] = len(bits)
            if order.executed.size:
                row["average_fill_price"] = float(order.executed.price)
                row["commission"] = float(order.executed.comm)
                row["executed_at"] = bt.num2date(order.executed.dt).date().isoformat()
            if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected, order.Expired):
                if self.pending_order is not None and self.pending_order.ref == order.ref:
                    self.pending_order = None

        def notify_trade(self, trade):
            if trade.isclosed:
                self.closed_trade_rows.append({
                    "opened_at": bt.num2date(trade.dtopen).date().isoformat(),
                    "closed_at": bt.num2date(trade.dtclose).date().isoformat(),
                    "gross_pnl": float(trade.pnl),
                    "net_pnl": float(trade.pnlcomm),
                })

    OrderLogReplay.__name__ = "OrderLogReplay"
    OrderLogReplay.__qualname__ = "OrderLogReplay"
    return OrderLogReplay


def _execution_order_projection(value: Any, *, strict: bool = False) -> Any:
    """Compare execution facts while allowing strategy-specific sizing fields."""
    if not isinstance(value, (tuple, list)):
        return value
    fields = (
        "order_ref", "side", "requested_qty", "status", "status_history",
        "decision_date", "filled_qty", "remaining_qty", "average_fill_price",
        "commission", "executed_at",
    )
    projected = []
    for row in value:
        if not isinstance(row, dict):
            return value
        # Trusted NL2 templates attach these sizing results to each order.
        # They are metadata from the frozen template; execution facts remain
        # compared through ``fields`` above. Python-upload orders do not emit
        # these fields, but both entrypoints share the same closed row schema.
        allowed_fields = {
            *fields, "reason", "sizing_reason", "target_shares",
            "sizing_requested_qty", "accepted_qty",
        }
        unknown_fields = row.keys() - allowed_fields
        if strict and unknown_fields:
            raise ReportReplayMismatch(
                "order row contains fields outside the verified execution schema: "
                + ", ".join(sorted(unknown_fields))
            )
        item = {field: row[field] for field in fields if field in row}
        # Backtrader's synthetic Created state is visible in the trusted
        # template report but not consistently emitted for worker decisions;
        # compare callback transitions, which begin at Submitted.
        if isinstance(item.get("status_history"), (tuple, list)):
            item["status_history"] = [status for status in item["status_history"] if status != "Created"]
        reason = row.get("reason", row.get("sizing_reason"))
        if reason is not None:
            item["reason"] = reason
        projected.append(item)
    return projected


def _matches(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-8)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_matches(left[key], right[key]) for key in left)
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(_matches(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _stable(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_stable(item) for item in value)
    return value
