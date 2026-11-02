"""Management permissions tied to one explicitly deployed PAPER draft."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_preparation import PaperOrderSetup
from src.session.models import Principal


class PaperDraftControlAuthorizer:
    """Derive narrow local control rights from the attributable deploy action.

    Entry controls end with the original entry window. Management controls
    remain bound to this account/operator/deployment while its composition is
    installed; actual cancel additionally checks the owned order and broker
    binding. Neither a management permission nor reconnect activates a strategy.
    """

    def __init__(self, *, scope: PaperRuntimeScope, principal: Principal, setup: PaperOrderSetup,
                 granted_at: datetime, now: Callable[[], datetime], fallback: Callable):
        if not isinstance(principal, Principal) or not principal.paper_operator:
            raise ValueError("an attributable deploying operator is required")
        if not isinstance(scope, PaperRuntimeScope) or not isinstance(setup, PaperOrderSetup):
            raise TypeError("a trusted scope and validated setup are required")
        if granted_at.tzinfo is None or granted_at.utcoffset() is None or granted_at >= setup.valid_until:
            raise ValueError("deployment authorization time is invalid")
        if not callable(now) or not callable(fallback):
            raise TypeError("clock and fallback authorizer must be callable")
        self.scope, self.principal, self.setup = scope, principal, setup
        self.granted_at, self.now, self.fallback = granted_at, now, fallback

    def __call__(self, principal: Principal, scope: PaperRuntimeScope, action: str) -> bool:
        if self.fallback(principal, scope, action) is True:
            return True
        if (not isinstance(principal, Principal) or not principal.paper_operator or scope != self.scope
                or (principal.subject, principal.auth_method, principal.tenant)
                != (self.principal.subject, self.principal.auth_method, self.principal.tenant)):
            return False
        current = self.now()
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None or current < self.granted_at:
            return False
        if action in {"start", "activate"}:
            return current < self.setup.valid_until and (action != "activate" or current >= self.setup.valid_from)
        return action in {"cancel", "recover", "stop"}
