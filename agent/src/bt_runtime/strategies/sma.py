"""Trusted template compiler and synthetic Backtrader runner.

This module accepts a frozen StrategySpec and compiles only the built-in
allowlisted templates. It does not load strategy source code. The runner is
intended for clearly synthetic, completed daily-bar fixtures and uses a real
Backtrader Cerebro instance with its standard BackBroker.
"""

from __future__ import annotations

import math
from datetime import timezone
import hashlib
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import backtrader as bt
import numpy as np
import pandas as pd

from src.execution.contracts import DataManifest, PositionMode, SourceKind, StrategySpec, canonical_sha256
from src.execution.position_sizing import size_target_fraction


TRUSTED_TEMPLATE_COMPILER_VERSION = "vibe-trusted-templates/2.0"
TRUSTED_TEMPLATE_SOURCE = Path(__file__).read_bytes()
TRUSTED_TEMPLATE_SOURCE_SHA256 = hashlib.sha256(TRUSTED_TEMPLATE_SOURCE).hexdigest()


@dataclass(frozen=True)
class CompiledStrategy:
    """Validated inputs for an allowlisted trusted template."""

    strategy_class: type[bt.Strategy]
    strategy_id: str
    template_id: str
    template_version: str
    fast_period: int | None
    slow_period: int | None
    allocation_fraction: float
    warmup_bars: int
    evaluation_start_index: int
    parameters: dict[str, int | float]


@dataclass(frozen=True)
class BacktestReport:
    """Deterministic result summary bound to its data manifest and provenance."""

    provenance: dict[str, Any]
    data_manifest: dict[str, Any]
    data_manifest_hash: str
    engine: str
    strategy_id: str
    template_id: str | None
    template_version: str | None
    instrument_id: str
    fast_period: int | None
    slow_period: int | None
    allocation_fraction: float | None
    initial_cash: float
    final_cash: float
    final_value: float
    evaluation_bar_count: int
    total_fees: float
    final_position: dict[str, float | int]
    equity_curve: tuple[dict[str, Any], ...]
    closed_trades: tuple[dict[str, Any], ...]
    metrics: dict[str, float | int]
    orders: tuple[dict[str, Any], ...]
    fills: tuple[dict[str, Any], ...]
    parameters: dict[str, int | float] | None = None
    source_kind: str = "natural_language_template"
    strategy_version_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly report suitable for deterministic comparison."""
        return asdict(self)


# Compatibility name retained for callers of the initial synthetic-only API.
SyntheticBacktestReport = BacktestReport


def compile_strategy(spec: StrategySpec | dict[str, Any]) -> CompiledStrategy:
    """Compile a validated StrategySpec into an allowlisted trusted strategy.

    The allowlisted template/version and exact parameter set are checked here
    because the shared contract schema intentionally supports more sources and
    future templates than this runtime slice.
    """
    if not isinstance(spec, StrategySpec):
        spec = StrategySpec.model_validate(spec)
    if spec.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE:
        raise ValueError("trusted compiler accepts natural_language_template only")
    if (spec.template_id, spec.template_version) == ("rsi_threshold", "1.0"):
        return _compile_rsi(spec)
    if (spec.template_id, spec.template_version) != ("sma_cross", "1.0"):
        raise ValueError("unsupported trusted strategy template or version")
    if spec.position_policy.mode is not PositionMode.TARGET_FRACTION:
        raise ValueError("sma_cross requires target_fraction sizing")
    if spec.position_policy.rebalance != "on_signal_transition":
        raise ValueError("sma_cross requires rebalance=on_signal_transition")
    if spec.position_policy.initial_entry != "wait_for_cross":
        raise ValueError("sma_cross requires initial_entry=wait_for_cross")
    expected_params = {"fast_period", "slow_period"}
    if set(spec.parameters) != expected_params:
        raise ValueError("sma_cross parameters must be exactly fast_period and slow_period")
    fast_period = spec.parameters["fast_period"]
    slow_period = spec.parameters["slow_period"]
    if type(fast_period) is not int or type(slow_period) is not int:
        raise ValueError("fast_period and slow_period must be integers")
    if fast_period < 1 or slow_period < 2 or fast_period >= slow_period:
        raise ValueError("periods must satisfy 1 <= fast_period < slow_period")
    fraction = spec.position_policy.allocation_fraction
    if fraction is None:  # The contract validator normally prevents this.
        raise ValueError("target_fraction requires allocation_fraction")

    strategy_class = _make_strategy_class(fast_period, slow_period, fraction)
    return CompiledStrategy(
        strategy_class=strategy_class,
        strategy_id=spec.strategy_id,
        template_id=spec.template_id,
        template_version=spec.template_version,
        fast_period=fast_period,
        slow_period=slow_period,
        allocation_fraction=float(fraction),
        warmup_bars=slow_period + 1,
        evaluation_start_index=slow_period,
        parameters={"fast_period": fast_period, "slow_period": slow_period},
    )


def wilder_rsi(closes: list[float], period: int) -> list[float | None]:
    """Return SMA-seeded Wilder RSI values, with 50 for a flat window.

    The first value is defined after ``period`` close-to-close changes. All
    inputs are required to be finite and positive so the function can also be
    used as a small, independently testable definition of the template math.
    """
    if type(period) is not int or not 2 <= period <= 250:
        raise ValueError("RSI period must be an integer from 2 through 250")
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("RSI closes must be finite positive numbers")
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(gain: float, loss: float) -> float:
        if gain == 0.0 and loss == 0.0:
            return 50.0
        if loss == 0.0:
            return 100.0
        if gain == 0.0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result[period] = value(avg_gain, avg_loss)
    for close_index in range(period + 1, len(closes)):
        change_index = close_index - 1
        avg_gain = ((period - 1) * avg_gain + gains[change_index]) / period
        avg_loss = ((period - 1) * avg_loss + losses[change_index]) / period
        result[close_index] = value(avg_gain, avg_loss)
    return result


def _threshold_equal(value: float, threshold: float) -> bool:
    """Treat floating-point noise around an exact RSI boundary as equality."""
    return math.isclose(value, threshold, rel_tol=0.0, abs_tol=1e-12)


def _compile_rsi(spec: StrategySpec) -> CompiledStrategy:
    if spec.position_policy.mode is not PositionMode.TARGET_FRACTION:
        raise ValueError("rsi_threshold requires target_fraction sizing")
    if spec.position_policy.rebalance != "on_signal_transition":
        raise ValueError("rsi_threshold requires rebalance=on_signal_transition")
    if spec.position_policy.initial_entry != "wait_for_cross":
        raise ValueError("rsi_threshold requires initial_entry=wait_for_cross")
    if set(spec.parameters) != {"period", "entry_below", "exit_above"}:
        raise ValueError("rsi_threshold parameters must be exactly period, entry_below, and exit_above")
    period = spec.parameters["period"]
    entry_below = spec.parameters["entry_below"]
    exit_above = spec.parameters["exit_above"]
    if type(period) is not int or not 2 <= period <= 250:
        raise ValueError("RSI period must be an integer from 2 through 250")
    if type(entry_below) not in (int, float) or type(exit_above) not in (int, float):
        raise ValueError("RSI thresholds must be finite numbers")
    entry_below, exit_above = float(entry_below), float(exit_above)
    if not math.isfinite(entry_below) or not math.isfinite(exit_above):
        raise ValueError("RSI thresholds must be finite numbers")
    if not (0 <= entry_below < exit_above <= 100):
        raise ValueError("RSI thresholds must satisfy 0 <= entry_below < exit_above <= 100")
    fraction = spec.position_policy.allocation_fraction
    if fraction is None:
        raise ValueError("target_fraction requires allocation_fraction")
    strategy_class = _make_rsi_strategy_class(period, entry_below, exit_above, fraction)
    return CompiledStrategy(
        strategy_class=strategy_class, strategy_id=spec.strategy_id,
        template_id=spec.template_id, template_version=spec.template_version,
        fast_period=None, slow_period=None, allocation_fraction=float(fraction),
        warmup_bars=period + 2,
        evaluation_start_index=period + 1,
        parameters={"period": period, "entry_below": entry_below, "exit_above": exit_above},
    )


def _make_rsi_strategy_class(
    period: int, entry_below: float, exit_above: float, allocation: Decimal
) -> type[bt.Strategy]:
    class RSIThresholdStrategy(bt.Strategy):
        params = (
            ("period", period), ("entry_below", entry_below), ("exit_above", exit_above),
            ("allocation_fraction", float(allocation)), ("allocation_fraction_decimal", allocation),
            ("strategy_budget", Decimal("40000")), ("fee_reserve", Decimal("25")),
            ("price_buffer_fraction", Decimal("0.01")),
        )

        def __init__(self) -> None:
            self.closes: list[float] = []
            self.avg_gain: float | None = None
            self.avg_loss: float | None = None
            self.previous_rsi: float | None = None
            self.pending_order = None
            self.order_rows: dict[int, dict[str, Any]] = {}
            self.fill_rows: list[dict[str, Any]] = []
            self.equity_rows: list[dict[str, Any]] = []
            self.closed_trade_rows: list[dict[str, Any]] = []

        def _record_equity(self) -> None:
            self.equity_rows.append({
                "bar": self.data.datetime.datetime(0).isoformat(),
                "cash": float(self.broker.getcash()), "equity": float(self.broker.getvalue()),
                "position_qty": int(self.position.size),
            })

        def prenext(self) -> None:
            self.next()

        def next(self) -> None:
            self._record_equity()
            close = float(self.data.close[0])
            if not math.isfinite(close) or close <= 0:
                return
            previous_close = self.closes[-1] if self.closes else None
            self.closes.append(close)
            if previous_close is None:
                return
            change = close - previous_close
            gain, loss = max(change, 0.0), max(-change, 0.0)
            if len(self.closes) <= self.p.period:
                return
            if self.avg_gain is None or self.avg_loss is None:
                changes = [self.closes[index] - self.closes[index - 1] for index in range(1, len(self.closes))]
                self.avg_gain = sum(max(delta, 0.0) for delta in changes) / self.p.period
                self.avg_loss = sum(max(-delta, 0.0) for delta in changes) / self.p.period
            else:
                self.avg_gain = ((self.p.period - 1) * self.avg_gain + gain) / self.p.period
                self.avg_loss = ((self.p.period - 1) * self.avg_loss + loss) / self.p.period
            if self.avg_gain == 0.0 and self.avg_loss == 0.0:
                current_rsi = 50.0
            elif self.avg_loss == 0.0:
                current_rsi = 100.0
            elif self.avg_gain == 0.0:
                current_rsi = 0.0
            else:
                current_rsi = 100.0 - 100.0 / (1.0 + self.avg_gain / self.avg_loss)
            previous_rsi, self.previous_rsi = self.previous_rsi, current_rsi
            if previous_rsi is None or self.pending_order is not None:
                return
            entered_from_at_or_above = previous_rsi > self.p.entry_below or _threshold_equal(previous_rsi, self.p.entry_below)
            strictly_below_entry = current_rsi < self.p.entry_below and not _threshold_equal(current_rsi, self.p.entry_below)
            exited_from_at_or_below = previous_rsi < self.p.exit_above or _threshold_equal(previous_rsi, self.p.exit_above)
            strictly_above_exit = current_rsi > self.p.exit_above and not _threshold_equal(current_rsi, self.p.exit_above)
            if not self.position and entered_from_at_or_above and strictly_below_entry:
                sizing = size_target_fraction(
                    entering_target=True, allocation_fraction=self.p.allocation_fraction_decimal,
                    account_equity=Decimal(str(self.broker.getvalue())),
                    strategy_budget=self.p.strategy_budget, cash_available=Decimal(str(self.broker.getcash())),
                    reference_price=Decimal(str(close)), fee_reserve=self.p.fee_reserve,
                    price_buffer_fraction=self.p.price_buffer_fraction, held_qty=int(self.position.size),
                )
                quantity = sizing.accepted_qty or 0
                if quantity > 0:
                    self.pending_order = self.buy(size=quantity)
                    self.order_rows[self.pending_order.ref] = {
                        "order_ref": self.pending_order.ref, "side": "BUY", "requested_qty": quantity,
                        "target_shares": sizing.target_shares, "sizing_requested_qty": sizing.requested_qty,
                        "accepted_qty": sizing.accepted_qty, "sizing_reason": sizing.reason.value,
                        "status": "Created", "status_history": ["Created"],
                        "decision_date": self.data.datetime.date(0).isoformat(),
                    }
            elif self.position and exited_from_at_or_below and strictly_above_exit:
                quantity = int(self.position.size)
                self.pending_order = self.sell(size=quantity)
                self.order_rows[self.pending_order.ref] = {
                    "order_ref": self.pending_order.ref, "side": "SELL", "requested_qty": quantity,
                    "sizing_requested_qty": quantity, "accepted_qty": quantity, "sizing_reason": "exit_position",
                    "status": "Created", "status_history": ["Created"],
                    "decision_date": self.data.datetime.date(0).isoformat(),
                }

        def notify_order(self, order: bt.Order) -> None:
            row = self.order_rows.get(order.ref)
            if row is None:
                row = {"order_ref": order.ref, "side": "BUY" if order.isbuy() else "SELL",
                       "requested_qty": abs(int(order.created.size)), "status_history": []}
                self.order_rows[order.ref] = row
            status = order.getstatusname()
            if not row["status_history"] or row["status_history"][-1] != status:
                row["status_history"].append(status)
            row["status"] = status
            row["filled_qty"] = abs(int(order.executed.size))
            row["remaining_qty"] = abs(int(order.executed.remsize))
            if order.status == order.Completed:
                executed_at = bt.num2date(order.executed.dt).date().isoformat()
                fill = {"order_ref": order.ref, "side": "BUY" if order.isbuy() else "SELL",
                        "qty": abs(int(order.executed.size)), "price": float(order.executed.price),
                        "commission": float(order.executed.comm), "executed_at": executed_at}
                self.fill_rows.append(fill)
                row.update({"average_fill_price": fill["price"], "commission": fill["commission"],
                            "executed_at": executed_at})
            if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected, order.Expired):
                if self.pending_order is not None and self.pending_order.ref == order.ref:
                    self.pending_order = None

        def notify_trade(self, trade: bt.Trade) -> None:
            if trade.isclosed:
                self.closed_trade_rows.append({
                    "opened_at": bt.num2date(trade.dtopen).date().isoformat(),
                    "closed_at": bt.num2date(trade.dtclose).date().isoformat(),
                    "gross_pnl": float(trade.pnl), "net_pnl": float(trade.pnlcomm),
                })

    RSIThresholdStrategy.__name__ = "TrustedRSIThresholdStrategy"
    RSIThresholdStrategy.__qualname__ = "TrustedRSIThresholdStrategy"
    return RSIThresholdStrategy


def _make_strategy_class(
    fast_period: int, slow_period: int, allocation: Decimal
) -> type[bt.Strategy]:
    class SMACrossStrategy(bt.Strategy):
        params = (
            ("fast_period", fast_period),
            ("slow_period", slow_period),
            ("allocation_fraction", float(allocation)),
            ("allocation_fraction_decimal", allocation),
            ("strategy_budget", Decimal("40000")),
            ("fee_reserve", Decimal("25")),
            ("price_buffer_fraction", Decimal("0.01")),
        )

        def __init__(self) -> None:
            self.fast = bt.indicators.SimpleMovingAverage(
                self.data.close, period=self.p.fast_period
            )
            self.slow = bt.indicators.SimpleMovingAverage(
                self.data.close, period=self.p.slow_period
            )
            self.pending_order = None
            self.order_rows: dict[int, dict[str, Any]] = {}
            self.fill_rows: list[dict[str, Any]] = []
            self.equity_rows: list[dict[str, Any]] = []
            self.closed_trade_rows: list[dict[str, Any]] = []

        def _record_equity(self) -> None:
            self.equity_rows.append(
                {
                    "bar": self.data.datetime.datetime(0).isoformat(),
                    "cash": float(self.broker.getcash()),
                    "equity": float(self.broker.getvalue()),
                    "position_qty": int(self.position.size),
                }
            )

        def prenext(self) -> None:
            # Record the warmup period too; it is part of the account-value
            # path even though no strategy decision is allowed yet.
            self._record_equity()

        def next(self) -> None:
            self._record_equity()
            # Need slow_period + 1 valid bars so both current and previous
            # differences exist. At the first valid point, no cross is inferred.
            if len(self) < self.p.slow_period + 1 or self.pending_order is not None:
                return

            previous_diff = float(self.fast[-1] - self.slow[-1])
            current_diff = float(self.fast[0] - self.slow[0])
            if not (math.isfinite(previous_diff) and math.isfinite(current_diff)):
                return

            if not self.position and previous_diff <= 0.0 and current_diff > 0.0:
                sizing = size_target_fraction(
                    entering_target=True,
                    allocation_fraction=self.p.allocation_fraction_decimal,
                    account_equity=Decimal(str(self.broker.getvalue())),
                    strategy_budget=self.p.strategy_budget,
                    cash_available=Decimal(str(self.broker.getcash())),
                    reference_price=Decimal(str(float(self.data.close[0]))),
                    fee_reserve=self.p.fee_reserve,
                    price_buffer_fraction=self.p.price_buffer_fraction,
                    held_qty=int(self.position.size),
                )
                quantity = sizing.accepted_qty or 0
                if quantity > 0:
                    self.pending_order = self.buy(size=quantity)
                    self.order_rows[self.pending_order.ref] = {
                        "order_ref": self.pending_order.ref,
                        "side": "BUY",
                        "requested_qty": quantity,
                        "target_shares": sizing.target_shares,
                        "sizing_requested_qty": sizing.requested_qty,
                        "accepted_qty": sizing.accepted_qty,
                        "sizing_reason": sizing.reason.value,
                        "status": "Created",
                        "status_history": ["Created"],
                        "decision_date": self.data.datetime.date(0).isoformat(),
                    }
            elif self.position and previous_diff >= 0.0 and current_diff < 0.0:
                quantity = int(self.position.size)
                sizing = size_target_fraction(
                    entering_target=False,
                    allocation_fraction=None,
                    account_equity=None,
                    strategy_budget=None,
                    cash_available=None,
                    reference_price=Decimal(str(float(self.data.close[0]))),
                    fee_reserve=None,
                    price_buffer_fraction=self.p.price_buffer_fraction,
                    held_qty=quantity,
                    sellable_qty=quantity,
                )
                quantity = sizing.accepted_qty or 0
                if quantity > 0:
                    self.pending_order = self.sell(size=quantity)
                    self.order_rows[self.pending_order.ref] = {
                        "order_ref": self.pending_order.ref,
                        "side": "SELL",
                        "requested_qty": quantity,
                        "sizing_requested_qty": sizing.requested_qty,
                        "accepted_qty": sizing.accepted_qty,
                        "sizing_reason": sizing.reason.value,
                        "status": "Created",
                        "status_history": ["Created"],
                        "decision_date": self.data.datetime.date(0).isoformat(),
                    }

        def notify_order(self, order: bt.Order) -> None:
            row = self.order_rows.get(order.ref)
            if row is None:
                # Defensive: every strategy-created order should be registered
                # synchronously in next() before Backtrader notifies it.
                row = {
                    "order_ref": order.ref,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "requested_qty": abs(int(order.created.size)),
                    "status_history": [],
                }
                self.order_rows[order.ref] = row
            status = order.getstatusname()
            if not row["status_history"] or row["status_history"][-1] != status:
                row["status_history"].append(status)
            row["status"] = status
            row["filled_qty"] = abs(int(order.executed.size))
            row["remaining_qty"] = abs(int(order.executed.remsize))
            if order.status == order.Completed:
                executed_at = bt.num2date(order.executed.dt).date().isoformat()
                fill = {
                    "order_ref": order.ref,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "qty": abs(int(order.executed.size)),
                    "price": float(order.executed.price),
                    "commission": float(order.executed.comm),
                    "executed_at": executed_at,
                }
                self.fill_rows.append(fill)
                row["average_fill_price"] = fill["price"]
                row["commission"] = fill["commission"]
                row["executed_at"] = executed_at
            if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected, order.Expired):
                if self.pending_order is not None and self.pending_order.ref == order.ref:
                    self.pending_order = None

        def notify_trade(self, trade: bt.Trade) -> None:
            if trade.isclosed:
                self.closed_trade_rows.append(
                    {
                        "opened_at": bt.num2date(trade.dtopen).date().isoformat(),
                        "closed_at": bt.num2date(trade.dtclose).date().isoformat(),
                        "gross_pnl": float(trade.pnl),
                        "net_pnl": float(trade.pnlcomm),
                    }
                )

    SMACrossStrategy.__name__ = "TrustedSMACrossStrategy"
    SMACrossStrategy.__qualname__ = "TrustedSMACrossStrategy"
    return SMACrossStrategy


def run_synthetic_backtest(
    spec: StrategySpec | dict[str, Any],
    synthetic_bars: pd.DataFrame,
    *,
    strategy_budget: Decimal,
    fee_reserve: Decimal,
    price_buffer_fraction: Decimal,
    initial_cash: float = 100_000.0,
    commission_rate: float = 0.001,
    slippage_fraction: float = 0.0,
) -> BacktestReport:
    """Run an allowlisted trusted template in real Cerebro/BackBroker on synthetic bars only.

    ``synthetic_bars`` must be an OHLCV DataFrame indexed by unique, increasing
    daily timestamps. Orders use BackBroker's Market execution, which fills at
    the next bar open. This function is an offline engine slice, not a broker
    adapter or paper/live execution path. Sizing inputs are explicit synthetic
    fixture values; they do not represent account authorization or production
    risk limits. Commission and percentage slippage use the same explicit
    BackBroker model as imported-data backtests.
    """
    normalized_spec = spec if isinstance(spec, StrategySpec) else StrategySpec.model_validate(spec)
    frame = _validate_bars(synthetic_bars)
    compiled = compile_strategy(normalized_spec)
    data_manifest = _build_synthetic_manifest(normalized_spec, frame, compiled.evaluation_start_index)
    return _run_trusted_backtest(
        normalized_spec, frame, data_manifest,
        strategy_budget=strategy_budget, fee_reserve=fee_reserve,
        price_buffer_fraction=price_buffer_fraction, initial_cash=initial_cash,
        commission_rate=commission_rate, slippage_fraction=slippage_fraction, source_label="synthetic_fixture",
    )


def run_verified_backtest(
    spec: StrategySpec | dict[str, Any],
    bars: pd.DataFrame,
    data_manifest: DataManifest | dict[str, Any],
    *,
    strategy_budget: Decimal,
    fee_reserve: Decimal,
    price_buffer_fraction: Decimal,
    initial_cash: float = 100_000.0,
    commission_rate: float = 0.001,
    slippage_fraction: float = 0.0,
) -> BacktestReport:
    """Run an allowlisted trusted template on imported, manifested bars.

    The report retains the historical report type for compatibility, but its
    provenance is ``provider_import`` and the manifest must be non-synthetic.
    Caller-declared provider/license and calendar provenance are not
    cryptographically authenticated by this offline function.
    """
    normalized_spec = spec if isinstance(spec, StrategySpec) else StrategySpec.model_validate(spec)
    if not isinstance(data_manifest, DataManifest):
        data_manifest = DataManifest.model_validate(data_manifest)
    frame = _validate_bars(bars)
    if data_manifest.synthetic:
        raise ValueError("verified backtest requires a non-synthetic data manifest")
    if data_manifest.instrument_id != normalized_spec.instrument_ids[0]:
        raise ValueError("data manifest instrument does not match StrategySpec")
    if data_manifest.range_end < data_manifest.range_start:
        raise ValueError("data manifest date range is invalid")
    local_dates = bars.index.tz_convert(normalized_spec.bar_policy.timezone).date
    if local_dates[0] < data_manifest.range_start or local_dates[-1] > data_manifest.range_end:
        raise ValueError("bars fall outside the data manifest date range")
    # Import lazily to avoid the market_data module's trusted-template
    # validation dependency becoming an import cycle.
    from src.bt_runtime.market_data import canonical_daily_bars_sha256

    expected_input_hash = data_manifest.quality_report.get("strategy_input_sha256")
    if not isinstance(expected_input_hash, str) or expected_input_hash != canonical_daily_bars_sha256(frame):
        raise ValueError("strategy input bars do not match the manifest strategy_input_sha256")
    return _run_trusted_backtest(
        normalized_spec, frame, data_manifest,
        strategy_budget=strategy_budget, fee_reserve=fee_reserve,
        price_buffer_fraction=price_buffer_fraction, initial_cash=initial_cash,
        commission_rate=commission_rate, slippage_fraction=slippage_fraction, source_label="provider_import",
    )


def _run_trusted_backtest(
    spec: StrategySpec,
    frame: pd.DataFrame,
    data_manifest: DataManifest,
    *,
    strategy_budget: Decimal,
    fee_reserve: Decimal,
    price_buffer_fraction: Decimal,
    initial_cash: float,
    commission_rate: float,
    slippage_fraction: float,
    source_label: str,
) -> BacktestReport:
    compiled = compile_strategy(spec)
    if len(frame) < compiled.warmup_bars:
        raise ValueError(f"data must include at least {compiled.warmup_bars} complete bars for warmup and a decision")
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial_cash must be a finite positive amount")
    if not math.isfinite(commission_rate) or commission_rate < 0:
        raise ValueError("commission_rate must be finite and non-negative")
    if not math.isfinite(slippage_fraction) or not 0 <= slippage_fraction <= 0.25:
        raise ValueError("slippage_fraction must be finite and between 0 and 0.25")
    for name, value in (
        ("strategy_budget", strategy_budget),
        ("fee_reserve", fee_reserve),
        ("price_buffer_fraction", price_buffer_fraction),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{name} must be a finite Decimal synthetic input")
    if strategy_budget <= 0 or fee_reserve < 0 or price_buffer_fraction < 0:
        raise ValueError("synthetic budget must be positive; fee reserve and price buffer must be non-negative")

    cerebro = bt.Cerebro(stdstats=False, preload=False, runonce=False)
    cerebro.broker.setcash(float(initial_cash))
    cerebro.broker.setcommission(commission=float(commission_rate))
    cerebro.broker.set_slippage_perc(
        perc=float(slippage_fraction), slip_open=True, slip_limit=True, slip_match=True, slip_out=False,
    )
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.adddata(bt.feeds.PandasData(dataname=frame, timeframe=bt.TimeFrame.Days))
    cerebro.addstrategy(
        compiled.strategy_class,
        strategy_budget=strategy_budget,
        fee_reserve=fee_reserve,
        price_buffer_fraction=price_buffer_fraction,
    )
    strategies = cerebro.run()
    strategy = strategies[0]
    position = strategy.getposition(strategy.data)
    data_manifest_data = data_manifest.model_dump(mode="json")
    data_manifest_hash = canonical_sha256(data_manifest)
    stable_refs = {row["order_ref"]: index for index, row in enumerate(strategy.order_rows.values(), start=1)}
    orders = tuple(
        _stable_value({**row, "order_ref": stable_refs[engine_ref]})
        for engine_ref, row in strategy.order_rows.items()
    )
    fills = tuple(
        _stable_value({**row, "order_ref": stable_refs[row["order_ref"]]})
        for row in strategy.fill_rows
    )
    drawdown = strategy.analyzers.drawdown.get_analysis()
    final_value = float(cerebro.broker.getvalue())
    session_dates = frame.index.tz_convert(spec.bar_policy.timezone).date
    evaluation_bar_count = sum(
        data_manifest.evaluation_start <= session_day <= data_manifest.evaluation_end
        for session_day in session_dates
    )
    if evaluation_bar_count < 1:
        raise ValueError("manifest evaluation window contains no Backtrader bars")
    annualized_return = (
        math.expm1(math.log(final_value / float(initial_cash)) / max(1, evaluation_bar_count - 1) * 252.0)
        if final_value > 0 else -1.0
    )
    max_drawdown_pct = float(drawdown.get("max", {}).get("drawdown", 0.0))
    max_drawdown_value = 0.0
    peak = float(initial_cash)
    for row in strategy.equity_rows:
        equity = float(row["equity"])
        peak = max(peak, equity)
        max_drawdown_value = max(max_drawdown_value, peak - equity)
    return BacktestReport(
        provenance={
            "data_source": source_label,
            "data_authenticity": data_manifest.quality_report.get(
                "provider_attestation", "not_applicable_synthetic_fixture"
            ),
            "data_manifest_hash": data_manifest_hash,
            "evaluation_window": {
                "warmup_start": data_manifest.warmup_start.isoformat(),
                "warmup_end": data_manifest.warmup_end.isoformat(),
                "evaluation_start": data_manifest.evaluation_start.isoformat(),
                "evaluation_end": data_manifest.evaluation_end.isoformat(),
            },
            "sizing": {
                "strategy_budget": str(strategy_budget),
                "fee_reserve": str(fee_reserve),
                "price_buffer_fraction": str(price_buffer_fraction),
            },
            "cost": {
                "commission_model": "BackBroker percentage commission",
                "commission_rate": str(commission_rate),
                "fee_reserve": str(fee_reserve),
                "slippage_model": "BackBroker percentage slippage at next-bar market open; capped to bar range",
                "slippage_fraction": str(slippage_fraction),
            },
        },
        data_manifest=data_manifest_data,
        data_manifest_hash=data_manifest_hash,
        engine=f"Backtrader {bt.__version__} / BackBroker",
        strategy_id=compiled.strategy_id,
        template_id=compiled.template_id,
        template_version=compiled.template_version,
        instrument_id=spec.instrument_ids[0] if isinstance(spec, StrategySpec) else str(spec["instrument_ids"][0]),
        fast_period=compiled.fast_period,
        slow_period=compiled.slow_period,
        allocation_fraction=compiled.allocation_fraction,
        initial_cash=float(initial_cash),
        final_cash=float(cerebro.broker.getcash()),
        final_value=final_value,
        evaluation_bar_count=evaluation_bar_count,
        total_fees=float(sum(fill["commission"] for fill in strategy.fill_rows)),
        final_position={"qty": int(position.size), "average_price": float(position.price)},
        equity_curve=tuple(_stable_value(row) for row in strategy.equity_rows),
        closed_trades=tuple(_stable_value(row) for row in strategy.closed_trade_rows),
        metrics={
            "total_return_fraction": (final_value / float(initial_cash)) - 1.0,
            "max_drawdown_fraction": max_drawdown_pct / 100.0,
            "max_drawdown_value": max_drawdown_value,
            "closed_trade_count": len(strategy.closed_trade_rows),
            "closed_trade_win_count": sum(row["net_pnl"] > 0 for row in strategy.closed_trade_rows),
            "backtrader_annualized_return": annualized_return,
        },
        orders=orders,
        fills=fills,
        parameters=compiled.parameters,
    )


def _validate_bars(bars: pd.DataFrame) -> pd.DataFrame:
    columns = ("open", "high", "low", "close", "volume")
    if not isinstance(bars, pd.DataFrame) or not all(column in bars.columns for column in columns):
        raise ValueError("bars must be a DataFrame with OHLCV columns")
    if len(bars) == 0 or not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("synthetic bars require a non-empty DatetimeIndex")
    if bars.index.tz is None:
        raise ValueError("bar timestamps must include a timezone")
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("bar timestamps must be unique and strictly increasing")
    values = bars.loc[:, columns].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("OHLCV values must be finite")
    if (values[["open", "high", "low", "close"]] <= 0).to_numpy().any():
        raise ValueError("equity prices must be positive")
    if (values["volume"] < 0).any():
        raise ValueError("volume must be non-negative")
    if (values["high"] < values[["open", "low", "close"]].max(axis=1)).any():
        raise ValueError("bar high must be at least open, low, and close")
    if (values["low"] > values[["open", "high", "close"]].min(axis=1)).any():
        raise ValueError("bar low must be at most open, high, and close")
    result = values.copy()
    result.index = bars.index
    return result


def _build_synthetic_manifest(
    spec: StrategySpec | dict[str, Any], bars: pd.DataFrame, evaluation_start_index: int
) -> DataManifest:
    """Create deterministic data provenance for an in-memory synthetic fixture."""
    if not isinstance(spec, StrategySpec):
        spec = StrategySpec.model_validate(spec)
    timezone_name = spec.bar_policy.timezone
    local_index = bars.index.tz_convert(timezone_name)
    csv_bytes = bars.to_csv(
        index=True,
        date_format="%Y-%m-%dT%H:%M:%S%z",
        float_format="%.10f",
        lineterminator="\n",
    ).encode("utf-8")
    warmup_end_index = max(0, evaluation_start_index - 1)
    dates = [stamp.date() for stamp in local_index]
    return DataManifest(
        manifest_id=f"synthetic-{hashlib.sha256(csv_bytes).hexdigest()[:16]}",
        provider="synthetic_fixture",
        instrument_id=spec.instrument_ids[0],
        currency="USD",
        fields=("open", "high", "low", "close", "volume"),
        range_start=dates[0],
        range_end=dates[-1],
        availability_model="completed_bar_available; synthetic timestamp fixtures",
        calendar_version=spec.bar_policy.calendar_id,
        adjustment_policy="synthetic_unadjusted",
        corporate_action_policy="not_applicable_synthetic_fixture",
        download_parameters={"format": "deterministic_csv", "float_format": "%.10f"},
        fetched_at=local_index[-1].to_pydatetime().astimezone(timezone.utc),
        file_sha256=hashlib.sha256(csv_bytes).hexdigest(),
        quality_report={"validation": "passed", "synthetic": True},
        warmup_start=dates[0],
        warmup_end=dates[warmup_end_index],
        evaluation_start=dates[evaluation_start_index],
        evaluation_end=dates[-1],
        synthetic=True,
        synthetic_note="generated deterministic unit fixture; fetched_at is the final fixture timestamp placeholder",
    )


def _stable_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, dict):
        return {key: _stable_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    return value
