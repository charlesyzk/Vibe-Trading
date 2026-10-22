"""Trusted, versioned strategy compilers for the Backtrader runtime."""

from .sma import (
    BacktestReport,
    CompiledStrategy,
    SyntheticBacktestReport,
    compile_strategy,
    run_synthetic_backtest,
    run_verified_backtest,
)

__all__ = [
    "BacktestReport",
    "CompiledStrategy",
    "SyntheticBacktestReport",
    "compile_strategy",
    "run_synthetic_backtest",
    "run_verified_backtest",
]
