"""Run a frozen Python upload through isolated event callbacks and real Backtrader.

Uploaded source never enters the Backtrader process. The trusted bridge releases
one completed bar and JSON checkpoint to a one-shot isolated worker, validates
the returned Market/DAY proposal, and submits it as a native Backtrader Order
to the real BackBroker. Explicit share quantities are never resized here.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import backtrader as bt
import pandas as pd

from src.bt_runtime.strategies import sma as sma_runtime
from src.execution.contracts import Bar, DataManifest, SourceKind, StrategySpec, canonical_sha256
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION, EventWorkerError, invoke_strategy_worker
from src.execution.event_journal import WorkerEventResult
from src.execution.python_sandbox import PythonSandbox
from src.execution.versions import StrategyVersionStore
from src.bt_runtime.report_replay import (
    ReportReplayMismatch,
    replay_backtrader_order_log_subprocess,
    verify_python_report_replay,
)

MAX_PYTHON_BACKTEST_EVENTS = 1_000


class PythonStrategyRunError(RuntimeError):
    """A frozen uploaded strategy or its proposal violated the runtime contract."""

    def __init__(self, message: str, *, event_seq: int | None = None, public_failure: dict[str, Any] | None = None):
        super().__init__(message)
        self.event_seq = event_seq
        self.public_failure = public_failure


@dataclass(frozen=True)
class PythonBacktestReport:
    """Shared report shape for an uploaded strategy executed by real Backtrader."""

    provenance: dict[str, Any]
    data_manifest: dict[str, Any]
    data_manifest_hash: str
    engine: str
    strategy_id: str
    template_id: None
    template_version: None
    instrument_id: str
    fast_period: None
    slow_period: None
    allocation_fraction: None
    initial_cash: float
    final_cash: float
    final_value: float
    total_fees: float
    final_position: dict[str, float | int]
    equity_curve: tuple[dict[str, Any], ...]
    closed_trades: tuple[dict[str, Any], ...]
    metrics: dict[str, float | int]
    orders: tuple[dict[str, Any], ...]
    fills: tuple[dict[str, Any], ...]
    decision_log: tuple[dict[str, Any], ...]
    evaluation_bar_count: int
    warmup_proposal_count: int
    execution_integrity: dict[str, Any]
    source_kind: str
    strategy_version_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_python_upload_backtest(
    spec: StrategySpec | dict[str, Any],
    bars: pd.DataFrame,
    data_manifest: DataManifest | dict[str, Any],
    *,
    version_id: str,
    versions: StrategyVersionStore,
    sandbox: PythonSandbox,
    bar_records: Sequence[Bar] | None = None,
    initial_cash: float = 100_000.0,
    commission_rate: float = 0.001,
    slippage_fraction: float = 0.0,
    event_coordinator: Any | None = None,
) -> PythonBacktestReport:
    """Run a frozen explicit-quantity upload with the real Cerebro/BackBroker.

    A real manifest must be accompanied by its imported ``Bar`` records, so
    decision input uses the exact verified availability timestamp and OHLCV
    that produced the feed. Synthetic fixtures may derive clearly labeled
    timestamp placeholders from their index.
    """
    normalized = spec if isinstance(spec, StrategySpec) else StrategySpec.model_validate(spec)
    if normalized.source_kind is not SourceKind.PYTHON_UPLOAD:
        raise PythonStrategyRunError("Python upload runner requires source_kind=python_upload")
    if normalized.position_policy.mode.value != "explicit_quantity":
        raise PythonStrategyRunError("Python uploads must use explicit_quantity sizing in V1")
    record = versions.get_version(version_id)
    if record.source_kind is not SourceKind.PYTHON_UPLOAD:
        raise PythonStrategyRunError("strategy version is not a Python upload")
    if record.spec_hash != canonical_sha256(normalized):
        raise PythonStrategyRunError("run StrategySpec does not match the frozen strategy version")
    if record.engine_version != f"backtrader/{bt.__version__}":
        raise PythonStrategyRunError("frozen strategy version targets a different Backtrader version")
    if record.compiler_version != PYTHON_WORKER_COMPILER_VERSION:
        raise PythonStrategyRunError("frozen strategy version targets an unsupported Python worker protocol")
    try:
        source = versions.read_upload_source(version_id).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PythonStrategyRunError("frozen strategy source must be valid UTF-8") from exc

    manifest = data_manifest if isinstance(data_manifest, DataManifest) else DataManifest.model_validate(data_manifest)
    frame = sma_runtime._validate_bars(bars)
    if len(frame) > MAX_PYTHON_BACKTEST_EVENTS:
        raise PythonStrategyRunError(
            f"Python upload backtests are limited to {MAX_PYTHON_BACKTEST_EVENTS} completed bars per run"
        )
    instrument_id = normalized.instrument_ids[0]
    if manifest.instrument_id != instrument_id:
        raise PythonStrategyRunError("data manifest instrument does not match StrategySpec")
    if manifest.synthetic is False and bar_records is None:
        raise PythonStrategyRunError("provider imports must include validated Bar records")
    if not manifest.synthetic:
        from src.bt_runtime.market_data import canonical_daily_bars_sha256

        expected_input_hash = manifest.quality_report.get("strategy_input_sha256")
        if not isinstance(expected_input_hash, str) or expected_input_hash != canonical_daily_bars_sha256(frame):
            raise PythonStrategyRunError("strategy input bars do not match the manifest strategy_input_sha256")
    dates = frame.index.tz_convert(normalized.bar_policy.timezone).date
    if dates[0] < manifest.range_start or dates[-1] > manifest.range_end:
        raise PythonStrategyRunError("bars fall outside the data manifest range")
    records = tuple(bar_records) if bar_records is not None else _synthetic_bar_records(normalized, frame)
    _validate_event_bars(records, frame, instrument_id, normalized.bar_policy.timezone)
    _validate_evaluation_window(manifest, records)
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial_cash must be finite and positive")
    if not math.isfinite(commission_rate) or commission_rate < 0:
        raise ValueError("commission_rate must be finite and non-negative")
    if not math.isfinite(slippage_fraction) or not 0 <= slippage_fraction <= 0.25:
        raise ValueError("slippage_fraction must be finite and between 0 and 0.25")

    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)
    cerebro.broker.setcash(float(initial_cash))
    cerebro.broker.setcommission(commission=float(commission_rate))
    cerebro.broker.set_slippage_perc(
        perc=float(slippage_fraction), slip_open=True, slip_limit=True, slip_match=True, slip_out=False,
    )
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.adddata(bt.feeds.PandasData(dataname=frame, timeframe=bt.TimeFrame.Days))
    cerebro.addstrategy(
        _make_python_strategy(
            source=source,
            version_hash=version_id,
            records=records,
            evaluation_start=manifest.evaluation_start,
            evaluation_end=manifest.evaluation_end,
            sandbox=sandbox,
            event_coordinator=event_coordinator,
        )
    )
    strategy = cerebro.run()[0]
    position = strategy.getposition(strategy.data)
    stable_refs = {row["order_ref"]: index for index, row in enumerate(strategy.order_rows.values(), start=1)}
    orders = tuple(_stable({**row, "order_ref": stable_refs[engine_ref]}) for engine_ref, row in strategy.order_rows.items())
    fills = tuple(_stable({**row, "order_ref": stable_refs[row["order_ref"]]}) for row in strategy.fill_rows)
    manifest_data = manifest.model_dump(mode="json")
    drawdown = strategy.analyzers.drawdown.get_analysis()
    returns = strategy.analyzers.returns.get_analysis()
    final_value = float(cerebro.broker.getvalue())
    peak = float(initial_cash)
    max_drawdown_value = 0.0
    for row in strategy.equity_rows:
        peak = max(peak, float(row["equity"]))
        max_drawdown_value = max(max_drawdown_value, peak - float(row["equity"]))
    report = PythonBacktestReport(
        provenance={
            "data_source": "synthetic_fixture" if manifest.synthetic else "provider_import",
            "data_authenticity": manifest.quality_report.get("provider_attestation", "not_attested"),
            "data_manifest_hash": canonical_sha256(manifest),
                "evaluation_window": {
                    "warmup_start": manifest.warmup_start.isoformat(),
                    "warmup_end": manifest.warmup_end.isoformat(),
                    "evaluation_start": manifest.evaluation_start.isoformat(),
                    "evaluation_end": manifest.evaluation_end.isoformat(),
                    "warmup_proposals_ignored": strategy.warmup_proposal_count,
                },
            "strategy_version_hash": version_id,
            "sizing": {"mode": "explicit_quantity", "host_resizes_requested_shares": False},
            "cost": {
                "commission_model": "BackBroker percentage commission",
                "commission_rate": str(commission_rate),
                "slippage_model": "BackBroker percentage slippage at next-bar market open; capped to bar range",
                "slippage_fraction": str(slippage_fraction),
            },
        },
        data_manifest=manifest_data,
        data_manifest_hash=canonical_sha256(manifest),
        engine=f"Backtrader {bt.__version__} / BackBroker",
        strategy_id=normalized.strategy_id,
        template_id=None,
        template_version=None,
        instrument_id=instrument_id,
        fast_period=None,
        slow_period=None,
        allocation_fraction=None,
        initial_cash=float(initial_cash),
        final_cash=float(cerebro.broker.getcash()),
        final_value=final_value,
        total_fees=float(sum(fill["commission"] for fill in strategy.fill_rows)),
        final_position={"qty": int(position.size), "average_price": float(position.price)},
        equity_curve=tuple(_stable(row) for row in strategy.equity_rows),
        closed_trades=tuple(_stable(row) for row in strategy.closed_trade_rows),
        metrics={
            "total_return_fraction": final_value / float(initial_cash) - 1.0,
            "max_drawdown_fraction": float(drawdown.get("max", {}).get("drawdown", 0.0)) / 100.0,
            "max_drawdown_value": max_drawdown_value,
            "closed_trade_count": len(strategy.closed_trade_rows),
            "closed_trade_win_count": sum(row["net_pnl"] > 0 for row in strategy.closed_trade_rows),
            # Backtrader's Returns analyzer sees warmup bars too. Keep the
            # same daily normalization formula but use evaluation periods only.
            "backtrader_annualized_return": _evaluation_annualized_return(
                initial_cash, final_value, strategy.evaluation_bar_count,
            ),
        },
        orders=orders,
        fills=fills,
        decision_log=tuple(_stable(row) for row in strategy.decision_rows),
        evaluation_bar_count=strategy.evaluation_bar_count,
        warmup_proposal_count=strategy.warmup_proposal_count,
        execution_integrity={},
        source_kind=SourceKind.PYTHON_UPLOAD.value,
        strategy_version_hash=version_id,
    )
    replay = replay_backtrader_order_log_subprocess(
        frame,
        records,
        report.decision_log,
        initial_cash=float(initial_cash),
        commission_rate=float(commission_rate),
        slippage_fraction=float(slippage_fraction),
        data_manifest_hash=canonical_sha256(manifest),
        instrument_id=instrument_id,
        evaluation_start=manifest.evaluation_start,
    )
    try:
        integrity = verify_python_report_replay(
            report.to_dict(),
            replay,
            initial_cash=float(initial_cash),
            commission_rate=float(commission_rate),
            slippage_fraction=float(slippage_fraction),
            data_manifest_hash=canonical_sha256(manifest),
            strategy_version_hash=version_id,
            engine=f"Backtrader {bt.__version__} / BackBroker",
            instrument_id=instrument_id,
            strict_order_shape=True,
        )
    except ReportReplayMismatch:
        if event_coordinator is not None:
            event_coordinator.journal.mark_paused(event_coordinator.run_id)
        raise
    if event_coordinator is not None:
        event_coordinator.journal.mark_complete(event_coordinator.run_id, expected_events=len(records))
        integrity.update({
            "event_journal": "durable_backtest_replay",
            "event_journal_state": "COMPLETE",
            "event_count": len(records),
            "reused_committed_results": event_coordinator.reused_results,
            "newly_computed_results": event_coordinator.computed_results,
            "broker_recovery": "rebuilt_by_full_replay_from_first_bar",
        })
    return replace(report, execution_integrity=integrity)


def _make_python_strategy(
    *,
    source: str,
    version_hash: str,
    records: tuple[Bar, ...],
    evaluation_start: date,
    evaluation_end: date,
    sandbox: PythonSandbox,
    event_coordinator: Any | None = None,
):
    class IsolatedPythonStrategy(bt.Strategy):
        def __init__(self):
            self.worker_state: dict[str, Any] = {}
            self.event_index = 0
            self.pending_order = None
            self.order_rows: dict[int, dict[str, Any]] = {}
            self.fill_rows: list[dict[str, Any]] = []
            self.decision_rows: list[dict[str, Any]] = []
            self.equity_rows: list[dict[str, Any]] = []
            self.closed_trade_rows: list[dict[str, Any]] = []
            self.reported_exec_bits: dict[int, int] = {}
            self.evaluation_bar_count = 0
            self.warmup_proposal_count = 0

        def _record_equity(self):
            if self.event_index >= len(records):
                return
            current_session = records[self.event_index].session_date
            if evaluation_start <= current_session <= evaluation_end:
                self.evaluation_bar_count += 1
            self.equity_rows.append({
                "bar": self.data.datetime.datetime(0).isoformat(),
                "cash": float(self.broker.getcash()),
                "equity": float(self.broker.getvalue()),
                "position_qty": int(self.position.size),
            })

        def prenext(self):
            self._process_current_event()

        def next(self):
            self._process_current_event()

        def _process_current_event(self):
            if self.event_index >= len(records):
                self._fail_run("Backtrader emitted more bars than the verified market-data records")
            bar = records[self.event_index]
            if bar.session_date > evaluation_end:
                self._fail_run("Backtrader event is outside the frozen evaluation window")
            self._record_equity()
            try:
                if event_coordinator is None:
                    result: WorkerEventResult = invoke_strategy_worker(
                        sandbox=sandbox,
                        source=source,
                        event_id=f"{version_hash}:{self.event_index + 1}",
                        bar=bar,
                        state=self.worker_state,
                    )
                else:
                    # Backtrader stores dates as UTC floating-point values and
                    # returns a naive datetime even when tz=UTC is supplied.
                    # Reattach the verified engine convention before handing
                    # the timestamp to the strict coordinator contract.
                    feed_timestamp = bt.num2date(self.data.datetime[0], tz=timezone.utc).replace(
                        tzinfo=timezone.utc
                    )
                    result = event_coordinator.process_event(
                        self.event_index + 1,
                        feed_available_at=feed_timestamp,
                    )
            except EventWorkerError as exc:
                raise PythonStrategyRunError(
                    f"isolated strategy event failed at seq {self.event_index + 1}: {exc}",
                    event_seq=self.event_index + 1,
                    public_failure=exc.public_failure,
                ) from exc
            self.worker_state = result.state
            self.event_index += 1
            proposals = result.order_proposals
            if len(proposals) > 1:
                self._fail_run("only one native order proposal is allowed per daily event")
            if not proposals:
                return
            if bar.session_date < evaluation_start:
                self.warmup_proposal_count += 1
                return
            if self.pending_order is not None:
                self._fail_run("strategy proposed a new order while a native order is still active")
            proposal = proposals[0]
            if proposal.side == "SELL" and proposal.integer_qty > int(self.position.size):
                self._fail_run("sell proposal exceeds the current long position; shorting is disabled")
            order = self.buy(size=proposal.integer_qty) if proposal.side == "BUY" else self.sell(size=proposal.integer_qty)
            self.pending_order = order
            self.decision_rows.append({
                "seq": self.event_index,
                "session_date": bar.session_date.isoformat(),
                "side": proposal.side,
                "requested_qty": proposal.integer_qty,
                "reason": proposal.reason,
            })
            self.order_rows[order.ref] = {
                "order_ref": order.ref,
                "side": proposal.side,
                "requested_qty": proposal.integer_qty,
                "reason": proposal.reason,
                "status": order.getstatusname(),
                "status_history": [order.getstatusname()],
                "decision_date": bar.session_date.isoformat(),
                "filled_qty": 0,
                "remaining_qty": proposal.integer_qty,
            }

        def _fail_run(self, message: str):
            if event_coordinator is not None:
                event_coordinator.journal.mark_paused(event_coordinator.run_id)
            raise PythonStrategyRunError(message)

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

    IsolatedPythonStrategy.__name__ = "IsolatedPythonStrategy"
    IsolatedPythonStrategy.__qualname__ = "IsolatedPythonStrategy"
    return IsolatedPythonStrategy


def _validate_evaluation_window(manifest: DataManifest, records: tuple[Bar, ...]) -> None:
    """Require the frozen feed to cover one ordered warmup/evaluation interval."""
    if not records:
        raise PythonStrategyRunError("evaluation window requires completed bars")
    if not (
        manifest.range_start == manifest.warmup_start
        <= manifest.warmup_end < manifest.evaluation_start
        <= manifest.evaluation_end == manifest.range_end
    ):
        raise PythonStrategyRunError("manifest warmup and evaluation dates are not an ordered covered window")
    dates = tuple(record.session_date for record in records)
    if dates[0] != manifest.warmup_start or dates[-1] != manifest.evaluation_end:
        raise PythonStrategyRunError("completed bars do not span the frozen warmup/evaluation window")
    if manifest.warmup_end not in dates or manifest.evaluation_start not in dates:
        raise PythonStrategyRunError("warmup end and evaluation start must be present in completed bars")
    if any(day < manifest.warmup_start or day > manifest.evaluation_end for day in dates):
        raise PythonStrategyRunError("completed bars fall outside the frozen warmup/evaluation window")


def _evaluation_annualized_return(initial_value: float, final_value: float, evaluation_bars: int) -> float:
    if evaluation_bars < 1 or not math.isfinite(initial_value) or initial_value <= 0:
        raise PythonStrategyRunError("evaluation return inputs are invalid")
    if not math.isfinite(final_value) or final_value <= 0:
        return -1.0
    periods = max(1, evaluation_bars - 1)
    return math.expm1(math.log(final_value / initial_value) / periods * 252.0)


def _synthetic_bar_records(spec: StrategySpec, frame: pd.DataFrame) -> tuple[Bar, ...]:
    zone = ZoneInfo(spec.bar_policy.timezone)
    records = []
    for timestamp, row in frame.iterrows():
        available_at = timestamp.to_pydatetime().astimezone(timezone.utc)
        records.append(Bar(
            instrument_id=spec.instrument_ids[0],
            session_date=available_at.astimezone(zone).date(),
            start_at=available_at - timedelta(microseconds=1),
            end_at=available_at,
            available_at=available_at,
            timezone=spec.bar_policy.timezone,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            currency="USD",
            is_final=True,
            source="synthetic_fixture",
            adjustment="synthetic_unadjusted",
        ))
    return tuple(records)


def _validate_event_bars(
    records: tuple[Bar, ...], frame: pd.DataFrame, instrument_id: str, timezone_name: str
) -> None:
    if len(records) != len(frame):
        raise PythonStrategyRunError("completed Bar records must match the Backtrader feed length")
    for index, (bar, (timestamp, row)) in enumerate(zip(records, frame.iterrows(), strict=True)):
        if bar.instrument_id != instrument_id or not bar.is_final:
            raise PythonStrategyRunError("worker input must contain final bars for the selected instrument")
        if bar.timezone != timezone_name:
            raise PythonStrategyRunError("Bar timezone does not match the frozen StrategySpec")
        if bar.available_at != timestamp.to_pydatetime().astimezone(timezone.utc):
            raise PythonStrategyRunError(f"Bar availability timestamp does not match feed at index {index}")
        if bar.session_date != timestamp.tz_convert(timezone_name).date():
            raise PythonStrategyRunError(f"Bar session date does not match feed at index {index}")
        for field in ("open", "high", "low", "close", "volume"):
            if float(getattr(bar, field)) != float(row[field]):
                raise PythonStrategyRunError(f"Bar {field} does not match Backtrader feed at index {index}")


def _stable(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value
