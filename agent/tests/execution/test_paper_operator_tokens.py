from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.execution.paper_operator_tokens import PaperOperatorTokenStore, main
from src.execution.contracts import Environment
from src.execution.paper_control_grants import PaperControlGrantStore
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.session.models import AuthMethod


def test_operator_token_is_hashed_at_rest_named_and_revocable(tmp_path):
    now = [1_000]
    database = tmp_path / "paper-operators.sqlite"
    store = PaperOperatorTokenStore(database, clock=lambda: now[0])
    token_id, token = store.issue("partner-alice", expires_at=2_000)
    assert store.has_active_subject("partner-alice") is True

    principal = store.authenticate(token)
    assert principal is not None
    assert principal.subject == "partner-alice"
    assert principal.auth_method is AuthMethod.LOCAL_OPERATOR_TOKEN
    assert principal.attributable is True

    with sqlite3.connect(database) as connection:
        stored_values = [str(value) for row in connection.execute("SELECT * FROM paper_operator_tokens") for value in row]
    assert token not in stored_values
    assert PaperOperatorTokenStore(database, clock=lambda: now[0]).revoke(token_id) is True
    assert store.authenticate(token) is None
    assert store.has_active_subject("partner-alice") is False
    assert store.revoke(token_id) is False


def test_expired_operator_token_is_not_authenticated(tmp_path):
    now = [100]
    store = PaperOperatorTokenStore(tmp_path / "paper-operators.sqlite", clock=lambda: now[0])
    _token_id, token = store.issue("partner-bob", expires_at=110)
    assert store.authenticate(token) is not None
    now[0] = 110
    assert store.authenticate(token) is None


def test_unknown_and_wrong_prefix_tokens_are_not_authenticated(tmp_path):
    store = PaperOperatorTokenStore(tmp_path / "paper-operators.sqlite")
    assert store.authenticate("shared-key-holder") is None
    assert store.authenticate("vtpo_unknown") is None


def test_local_admin_cli_can_issue_exact_scope_action_grant(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    operators = PaperOperatorTokenStore()
    _alice_id, alice_token = operators.issue("partner-alice")
    _admin_id, admin_token = operators.issue("workspace-admin")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: admin_token)
    start = datetime.now(timezone.utc) - timedelta(seconds=5)
    until = start + timedelta(hours=1)

    assert main([
        "grant", "--subject", "partner-alice", "--issuer-subject", "workspace-admin",
        "--account-ref", "DUPAPER1", "--action", "activate",
        "--valid-from", start.isoformat(), "--valid-until", until.isoformat(),
    ]) == 0
    grant_id = capsys.readouterr().out.splitlines()[0].split("=", 1)[1]
    # The grant is bound to the provisioned recipient, action, and one account.
    recipient = operators.authenticate(alice_token)
    assert recipient is not None and recipient.subject == "partner-alice"
    scope = PaperRuntimeScope("IBKR", "DUPAPER1", Environment.PAPER)
    grants = PaperControlGrantStore(tmp_path / "paper-control-grants.sqlite")
    assert grants(recipient, scope, "activate") is True
    assert grants(recipient, scope, "start") is False
    assert grants(recipient, PaperRuntimeScope("IBKR", "DUPAPER2", Environment.PAPER), "activate") is False
    assert grant_id
