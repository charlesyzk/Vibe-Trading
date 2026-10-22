"""Durable, fail-closed PAPER management grants.

This store is intended for a trusted deployment/control plane to provision
explicit grants. It deliberately exposes no HTTP/API surface and does not
manufacture an attributable Principal from the current shared-key auth mode.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.execution.paper_execution_manager import PaperRuntimeScope
from src.session.models import Principal


PAPER_CONTROL_ACTIONS = frozenset({"start", "activate", "recover", "cancel", "stop"})


@dataclass(frozen=True, slots=True)
class PaperControlGrant:
    grant_id: str
    subject: str
    auth_method: str
    tenant: str | None
    broker: str
    account_ref: str
    environment: str
    action: str
    valid_from: int
    valid_until: int
    issued_by: str
    issuer_auth_method: str
    issuer_tenant: str | None


class PaperControlGrantStore:
    """SQLite-backed exact-scope authorizer for PAPER management actions.

    Grant issuance is a trusted-code operation, not an API operation. The
    caller must enforce its own grant-administration policy before calling
    :meth:`issue`; this store only persists the grant and evaluates it.
    """

    def __init__(self, database: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.database = Path(database).expanduser().resolve()
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def issue(
        self,
        *,
        principal: Principal,
        scope: PaperRuntimeScope,
        action: str,
        valid_from: int,
        valid_until: int,
        issued_by: Principal,
    ) -> PaperControlGrant:
        """Persist one narrow grant from a trusted provisioning caller."""
        if not isinstance(principal, Principal) or not principal.attributable:
            raise ValueError("PAPER grants require an attributable principal")
        if not isinstance(issued_by, Principal) or not issued_by.attributable:
            raise ValueError("PAPER grants require an attributable issuer")
        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a PaperRuntimeScope")
        if action not in PAPER_CONTROL_ACTIONS:
            raise ValueError("unsupported PAPER control action")
        if isinstance(valid_from, bool) or not isinstance(valid_from, int):
            raise TypeError("valid_from must be an integer Unix timestamp")
        if isinstance(valid_until, bool) or not isinstance(valid_until, int):
            raise TypeError("valid_until must be an integer Unix timestamp")
        if valid_from < 0 or valid_until <= valid_from:
            raise ValueError("grant validity window must be a positive, ordered interval")

        grant = PaperControlGrant(
            grant_id=str(uuid.uuid4()),
            subject=principal.subject,
            auth_method=principal.auth_method.value,
            tenant=principal.tenant,
            broker=scope.broker,
            account_ref=scope.account_ref,
            environment=scope.environment.value,
            action=action,
            valid_from=valid_from,
            valid_until=valid_until,
            issued_by=issued_by.subject,
            issuer_auth_method=issued_by.auth_method.value,
            issuer_tenant=issued_by.tenant,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paper_control_grants "
                "(grant_id, subject, auth_method, tenant, broker, account_ref, environment, action, "
                "valid_from, valid_until, issued_by, issuer_auth_method, issuer_tenant, issued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.grant_id, grant.subject, grant.auth_method, grant.tenant, grant.broker,
                    grant.account_ref, grant.environment, grant.action,
                    grant.valid_from, grant.valid_until, grant.issued_by,
                    grant.issuer_auth_method, grant.issuer_tenant,
                    int(self._clock()),
                ),
            )
        return grant

    def revoke(self, grant_id: str, *, revoked_by: Principal) -> bool:
        """Revoke one grant; returns False when it is absent or already revoked."""
        if not isinstance(revoked_by, Principal) or not revoked_by.attributable:
            raise ValueError("PAPER grant revocation requires an attributable principal")
        if not isinstance(grant_id, str) or not grant_id:
            raise ValueError("grant_id is required")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_control_grants SET revoked_at = ?, revoked_by = ? "
                "WHERE grant_id = ? AND revoked_at IS NULL",
                (int(self._clock()), revoked_by.subject, grant_id),
            )
        return cursor.rowcount == 1

    def allows(self, principal: Principal, scope: PaperRuntimeScope, action: str) -> bool:
        """Return True only for an exact, currently valid, non-revoked grant."""
        if (
            not isinstance(principal, Principal)
            or not principal.attributable
            or not isinstance(scope, PaperRuntimeScope)
            or action not in PAPER_CONTROL_ACTIONS
        ):
            return False
        now = int(self._clock())
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM paper_control_grants WHERE subject = ? AND auth_method = ? AND tenant IS ? "
                    "AND broker = ? AND account_ref = ? AND environment = ? AND action = ? "
                    "AND valid_from <= ? AND valid_until > ? AND revoked_at IS NULL LIMIT 1",
                    (
                        principal.subject, principal.auth_method.value, principal.tenant, scope.broker,
                        scope.account_ref, scope.environment.value, action, now, now,
                    ),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            # Authorization storage failures always deny.
            return False

    def __call__(self, principal: Principal, scope: PaperRuntimeScope, action: str) -> bool:
        return self.allows(principal, scope, action)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS paper_control_grants ("
                "grant_id TEXT PRIMARY KEY, subject TEXT NOT NULL, auth_method TEXT NOT NULL, tenant TEXT, "
                "broker TEXT NOT NULL, account_ref TEXT NOT NULL, environment TEXT NOT NULL, "
                "action TEXT NOT NULL CHECK(action IN ('start','activate','recover','cancel','stop')), "
                "valid_from INTEGER NOT NULL, valid_until INTEGER NOT NULL, issued_by TEXT NOT NULL, "
                "issuer_auth_method TEXT NOT NULL, issuer_tenant TEXT, "
                "issued_at INTEGER NOT NULL, revoked_at INTEGER, revoked_by TEXT, "
                "CHECK(valid_from >= 0 AND valid_until > valid_from), "
                "CHECK((revoked_at IS NULL) = (revoked_by IS NULL)))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS paper_control_grants_lookup "
                "ON paper_control_grants(subject, auth_method, tenant, broker, account_ref, environment, action, valid_until)"
            )
        try:
            self.database.chmod(0o600)
        except OSError:
            pass


__all__ = ["PAPER_CONTROL_ACTIONS", "PaperControlGrant", "PaperControlGrantStore"]
