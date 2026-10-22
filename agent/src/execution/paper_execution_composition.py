"""Typed, side-effect-free app-level PAPER composition declaration."""

from __future__ import annotations

from dataclasses import dataclass

from src.execution.contracts import is_sha256
from src.execution.paper_execution_manager import PaperExecutionManager, PaperRuntimeScope
from src.execution.paper_execution_readiness import PaperExecutionReadiness


@dataclass(frozen=True, slots=True)
class PaperExecutionComposition:
    """Trusted app-root inputs installed before serving requests.

    Creating or installing this value only registers already-constructed
    collaborators. It never starts a runtime or opens an IBKR connection.
    The readiness declaration remains a startup gate, not proof that its
    collaborators or external facts are authentic.
    """

    manager: PaperExecutionManager
    readiness: PaperExecutionReadiness
    scope: PaperRuntimeScope

    def __post_init__(self) -> None:
        if not isinstance(self.manager, PaperExecutionManager):
            raise TypeError("manager must be a PaperExecutionManager")
        if not isinstance(self.readiness, PaperExecutionReadiness):
            raise TypeError("readiness must be a PaperExecutionReadiness")
        if not isinstance(self.scope, PaperRuntimeScope):
            raise TypeError("scope must be a trusted PaperRuntimeScope")
        runtime_factory = self.manager.runtime_factory
        strategy_version_id = getattr(runtime_factory, "strategy_version_id", None)
        if not isinstance(strategy_version_id, str) or not is_sha256(strategy_version_id):
            raise ValueError(
                "PAPER app composition runtime factory must pin a lowercase SHA-256 strategy version"
            )


__all__ = ["PaperExecutionComposition"]
