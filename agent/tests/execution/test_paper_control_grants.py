from __future__ import annotations

from pathlib import Path

import pytest

from src.execution.contracts import Environment
from src.execution.paper_control_grants import PaperControlGrantStore
from src.execution.paper_execution_manager import (
    PaperExecutionManager,
    PaperRuntimeAuthorizationError,
    PaperRuntimeScope,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.session.models import AuthMethod, Principal


def principal(subject: str = "alice", *, tenant: str | None = None) -> Principal:
    return Principal(subject, AuthMethod.FEDERATED_IDENTITY, tenant=tenant)


def scope(account: str = "DU-PAPER-1") -> PaperRuntimeScope:
    return PaperRuntimeScope("IBKR", account, Environment.PAPER)


def test_grant_matches_exact_actor_scope_action_and_survives_reopen(tmp_path: Path):
    now = [1_000]
    database = tmp_path / "control-grants.sqlite3"
    store = PaperControlGrantStore(database, clock=lambda: now[0])
    store.issue(
        principal=principal("alice", tenant="team-a"),
        scope=scope(),
        action="start",
        valid_from=900,
        valid_until=1_100,
        issued_by=principal("admin", tenant="team-a"),
    )

    reopened = PaperControlGrantStore(database, clock=lambda: now[0])
    assert reopened(principal("alice", tenant="team-a"), scope(), "start") is True
    assert reopened(principal("bob", tenant="team-a"), scope(), "start") is False
    assert reopened(principal("alice", tenant="team-b"), scope(), "start") is False
    assert reopened(principal("alice", tenant="team-a"), scope("DU-PAPER-2"), "start") is False
    assert reopened(principal("alice", tenant="team-a"), scope(), "activate") is False
    assert reopened(
        Principal("alice", AuthMethod.SHARED_KEY, tenant="team-a"), scope(), "start"
    ) is False


def test_grant_validity_is_half_open_and_revocation_is_durable(tmp_path: Path):
    now = [100]
    database = tmp_path / "control-grants.sqlite3"
    store = PaperControlGrantStore(database, clock=lambda: now[0])
    grant = store.issue(
        principal=principal(), scope=scope(), action="stop",
        valid_from=100, valid_until=110, issued_by=principal("admin"),
    )
    assert store(principal(), scope(), "stop") is True
    now[0] = 110
    assert store(principal(), scope(), "stop") is False
    now[0] = 105
    assert PaperControlGrantStore(database, clock=lambda: now[0]).revoke(
        grant.grant_id, revoked_by=principal("admin")
    ) is True
    assert PaperControlGrantStore(database, clock=lambda: now[0])(principal(), scope(), "stop") is False
    assert store.revoke(grant.grant_id, revoked_by=principal("admin")) is False


@pytest.mark.parametrize(
    "valid_from,valid_until",
    [(0, 0), (-1, 10), (10, 10), (11, 10)],
)
def test_invalid_grant_window_is_rejected(tmp_path: Path, valid_from: int, valid_until: int):
    store = PaperControlGrantStore(tmp_path / "control-grants.sqlite3")
    with pytest.raises(ValueError, match="validity window"):
        store.issue(
            principal=principal(), scope=scope(), action="start",
            valid_from=valid_from, valid_until=valid_until, issued_by=principal("admin"),
        )


def test_unauthorized_start_is_denied_before_runtime_factory(tmp_path: Path):
    store = PaperControlGrantStore(tmp_path / "control-grants.sqlite3", clock=lambda: 1_000)
    side_effects: list[str] = []
    manager = PaperExecutionManager(
        runtime_factory=lambda _scope: side_effects.append("factory"),
        control_authorizer=store,
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )

    with pytest.raises(PaperRuntimeAuthorizationError):
        manager.start_for_principal(scope(), principal())
    with pytest.raises(PaperRuntimeAuthorizationError):
        manager.start_for_principal(scope(), Principal("shared", AuthMethod.SHARED_KEY))

    assert side_effects == []
    assert manager.status() is None
