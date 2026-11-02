"""Persist the original authorization time for an exact PAPER draft deployment."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.execution.contracts import Environment, canonical_sha256
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_preparation import PaperOrderSetup
from src.session.models import Principal


def deployment_granted_at(*, database: Path, scope: PaperRuntimeScope, principal: Principal,
                          draft: dict, now: datetime) -> datetime:
    """Remember an attributable deployment request; never invent a past grant.

    Restoring a receipt does not start a runtime or authorize a new order.
    The risk grant keeps its original entry window and management actions
    retain their independent authorizer.
    """
    if (not isinstance(scope, PaperRuntimeScope) or scope.broker != "IBKR"
            or scope.environment is not Environment.PAPER or not scope.account_ref.startswith("DU")):
        raise ValueError("receipt requires a trusted IBKR PAPER scope")
    if not isinstance(principal, Principal) or not principal.paper_operator:
        raise ValueError("receipt requires an attributable operator")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("receipt clock must be timezone aware")
    now = now.astimezone(timezone.utc)
    setup = PaperOrderSetup.model_validate(draft["setup"])
    identity = canonical_sha256({"draft": draft, "broker": scope.broker, "account": scope.account_ref,
                                 "environment": scope.environment.value, "subject": principal.subject,
                                 "auth_method": principal.auth_method.value, "tenant": principal.tenant})
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database, timeout=10, isolation_level=None) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS paper_deployment_receipt ("
                           "identity TEXT PRIMARY KEY, granted_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS paper_deployment_audit ("
                           "identity TEXT PRIMARY KEY, subject TEXT NOT NULL, auth_method TEXT NOT NULL, tenant TEXT, "
                           "broker TEXT NOT NULL, account_ref TEXT NOT NULL, environment TEXT NOT NULL, granted_at TEXT NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT granted_at FROM paper_deployment_receipt WHERE identity = ?", (identity,)).fetchone()
        if row is None:
            if now >= setup.valid_until:
                connection.rollback()
                raise ValueError("an expired draft cannot receive a new deployment authorization")
            granted_at = now
            connection.execute("INSERT INTO paper_deployment_receipt(identity, granted_at) VALUES (?, ?)",
                               (identity, granted_at.isoformat()))
        else:
            granted_at = datetime.fromisoformat(row[0])
            if (granted_at.tzinfo is None or granted_at.utcoffset() is None
                    or granted_at > now or granted_at >= setup.valid_until):
                connection.rollback()
                raise ValueError("deployment authorization receipt is invalid")
            granted_at = granted_at.astimezone(timezone.utc)
        audit = (principal.subject, principal.auth_method.value, principal.tenant, scope.broker,
                 scope.account_ref, scope.environment.value, granted_at.isoformat())
        existing_audit = connection.execute(
            "SELECT subject,auth_method,tenant,broker,account_ref,environment,granted_at "
            "FROM paper_deployment_audit WHERE identity=?", (identity,),
        ).fetchone()
        if existing_audit is None:
            connection.execute("INSERT INTO paper_deployment_audit VALUES (?,?,?,?,?,?,?,?)", (identity, *audit))
        elif existing_audit != audit:
            connection.rollback()
            raise ValueError("deployment audit identity is invalid")
        connection.commit()
    try:
        database.chmod(0o600)
    except OSError:
        pass
    return granted_at
