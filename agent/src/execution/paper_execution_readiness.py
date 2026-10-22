"""Explicit, non-secret readiness declaration for app-level PAPER wiring.

This object is diagnostic metadata supplied by the trusted application
composition root. It does not authorize control actions or make a runtime
ready by itself; the manager and its runtime checks remain authoritative.
Missing capabilities are reported with stable, non-sensitive codes so the UI
can distinguish a registered low-level factory from a complete strategy
session composition.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaperExecutionReadiness:
    """Capabilities the app composition root has explicitly installed."""

    strategy_session_builder: bool = False
    attributable_control_authorizer: bool = False
    account_and_reconciliation_facts: bool = False
    market_data_and_verified_contracts: bool = False
    risk_and_cost_facts: bool = False

    def __post_init__(self) -> None:
        for name in (
            "strategy_session_builder",
            "attributable_control_authorizer",
            "account_and_reconciliation_facts",
            "market_data_and_verified_contracts",
            "risk_and_cost_facts",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")

    @property
    def missing_requirements(self) -> tuple[str, ...]:
        requirements = (
            ("strategy_session_builder", self.strategy_session_builder),
            ("attributable_control_authorizer", self.attributable_control_authorizer),
            ("account_reconciliation_facts", self.account_and_reconciliation_facts),
            ("market_data_verified_contracts", self.market_data_and_verified_contracts),
            ("risk_cost_facts", self.risk_and_cost_facts),
        )
        return tuple(code for code, installed in requirements if not installed)

    @property
    def composition_ready(self) -> bool:
        return not self.missing_requirements


__all__ = ["PaperExecutionReadiness"]
