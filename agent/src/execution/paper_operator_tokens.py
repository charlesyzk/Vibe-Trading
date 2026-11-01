"""Locally issued, individually attributable PAPER operator credentials.

Tokens are high-entropy bearer secrets. Only SHA-256 digests are persisted;
the plaintext token is returned once to the trusted provisioning caller.
Shared API keys and loopback trust remain separate and never acquire a human
identity through this store.
"""

from __future__ import annotations

import hashlib
import argparse
import getpass
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from src.config.paths import get_runtime_root
from src.session.models import AuthMethod, Principal


TOKEN_PREFIX = "vtpo_"
DATABASE_NAME = "paper-operators.sqlite"
_PAPER_ACCOUNT_RE = re.compile(r"^DU[A-Z0-9]+$")


class PaperOperatorTokenStore:
    """Persist named operator tokens with exact, revocable identity bindings."""

    def __init__(
        self, database: str | Path | None = None, *, clock=time.time, initialize: bool = True,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.database = (
            Path(database).expanduser().resolve()
            if database is not None
            else (get_runtime_root() / DATABASE_NAME).resolve()
        )
        self._clock = clock
        if initialize:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def issue(self, subject: str, *, expires_at: int | None = None) -> tuple[str, str]:
        """Issue a token and return ``(token_id, plaintext_token)`` exactly once."""
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            raise ValueError("operator subject must contain 1 to 200 characters")
        if expires_at is not None and (type(expires_at) is not int or expires_at <= int(self._clock())):
            raise ValueError("expires_at must be a future Unix timestamp")
        token_id = str(uuid.uuid4())
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = self._digest(token)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO paper_operator_tokens "
                "(token_id, subject, token_sha256, created_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (token_id, subject.strip(), digest, int(self._clock()), expires_at),
            )
        return token_id, token

    def authenticate(self, token: str) -> Principal | None:
        """Resolve a presented token to its named operator, or return ``None``."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return None
        if not self.database.is_file():
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT subject FROM paper_operator_tokens "
                    "WHERE token_sha256 = ? AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                    (self._digest(token), int(self._clock())),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return Principal(subject=row[0], auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN)

    def revoke(self, token_id: str) -> bool:
        """Revoke one credential without deleting its audit record."""
        if not isinstance(token_id, str) or not token_id.strip():
            raise ValueError("token_id is required")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_operator_tokens SET revoked_at = ? "
                "WHERE token_id = ? AND revoked_at IS NULL",
                (int(self._clock()), token_id.strip()),
            )
        return cursor.rowcount == 1

    def has_active_subject(self, subject: str) -> bool:
        """Return whether a named operator has an unexpired active token."""
        if not isinstance(subject, str) or not subject.strip():
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM paper_operator_tokens WHERE subject=? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (subject.strip(), int(self._clock())),
            ).fetchone()
        return row is not None

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS paper_operator_tokens ("
                "token_id TEXT PRIMARY KEY, subject TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, "
                "created_at INTEGER NOT NULL, expires_at INTEGER, revoked_at INTEGER, "
                "CHECK(length(subject) BETWEEN 1 AND 200), "
                "CHECK(expires_at IS NULL OR expires_at > created_at))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS paper_operator_tokens_active "
                "ON paper_operator_tokens(token_sha256, expires_at, revoked_at)"
            )
        try:
            self.database.chmod(0o600)
        except OSError:
            pass


def authenticate_paper_operator_token(token: str) -> Principal | None:
    """Authenticate from the user-level token database without creating it."""
    database = get_runtime_root() / DATABASE_NAME
    if not database.is_file():
        return None
    try:
        return PaperOperatorTokenStore(database, initialize=False).authenticate(token)
    except (OSError, sqlite3.Error, ValueError):
        return None


def _parse_expiry(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expiry must be ISO-8601 with an explicit timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("expiry must include a timezone, such as +00:00")
    return int(parsed.timestamp())


def _parse_required_time(value: str) -> int:
    parsed = _parse_expiry(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("a timezone-aware ISO-8601 timestamp is required")
    return parsed


def main(argv: list[str] | None = None) -> int:
    """Provision or revoke an operator token from the trusted local shell."""
    from src.execution.contracts import Environment
    from src.execution.paper_control_grants import PAPER_CONTROL_ACTIONS, PaperControlGrantStore
    from src.execution.paper_execution_manager import PaperRuntimeScope

    parser = argparse.ArgumentParser(description="Manage individually attributable IBKR PAPER operators")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="issue one named operator token")
    issue.add_argument("--subject", required=True, help="stable partner/operator identifier")
    issue.add_argument("--expires-at", type=_parse_expiry, help="optional ISO-8601 expiry with timezone")
    revoke = commands.add_parser("revoke", help="revoke a token by its non-secret token id")
    revoke.add_argument("--token-id", required=True)
    grant = commands.add_parser("grant", help="grant one operator an exact PAPER control action")
    grant.add_argument("--subject", required=True, help="operator who receives the grant")
    grant.add_argument("--issuer-subject", required=True, help="active operator who issues this grant")
    grant.add_argument("--account-ref", required=True, help="the exact DU paper account")
    grant.add_argument("--action", required=True, choices=sorted(PAPER_CONTROL_ACTIONS))
    grant.add_argument("--valid-from", required=True, type=_parse_required_time)
    grant.add_argument("--valid-until", required=True, type=_parse_required_time)
    revoke_grant = commands.add_parser("revoke-grant", help="revoke a grant by its non-secret grant id")
    revoke_grant.add_argument("--grant-id", required=True)
    revoke_grant.add_argument("--issuer-subject", required=True, help="active operator who revokes this grant")
    args = parser.parse_args(argv)
    store = PaperOperatorTokenStore()
    if args.command == "issue":
        token_id, token = store.issue(args.subject, expires_at=args.expires_at)
        print(f"token_id={token_id}")
        print(f"operator_token={token}")
        print("The token is shown once. Store it as that operator's Bearer credential.")
        return 0
    if args.command == "revoke":
        revoked = store.revoke(args.token_id)
        print("revoked" if revoked else "not_found_or_already_revoked")
        return 0 if revoked else 1

    issuer = store.authenticate(getpass.getpass("Issuing operator token: "))
    if issuer is None or issuer.subject != args.issuer_subject:
        parser.error("issuer token is invalid or does not match issuer-subject")
    grants = PaperControlGrantStore(get_runtime_root() / "paper-control-grants.sqlite")
    if args.command == "grant":
        account_ref = args.account_ref.strip().upper()
        if not _PAPER_ACCOUNT_RE.fullmatch(account_ref):
            parser.error("account-ref must be a valid DU PAPER account reference")
        if not store.has_active_subject(args.subject):
            parser.error("subject must have an active local operator token")
        recipient = Principal(args.subject, AuthMethod.LOCAL_OPERATOR_TOKEN)
        grant = grants.issue(
            principal=recipient,
            scope=PaperRuntimeScope("IBKR", account_ref, Environment.PAPER),
            action=args.action,
            valid_from=args.valid_from,
            valid_until=args.valid_until,
            issued_by=issuer,
        )
        print(f"grant_id={grant.grant_id}")
        print(f"subject={grant.subject} action={grant.action} environment=paper")
        return 0

    if args.command == "revoke-grant":
        revoked = grants.revoke(args.grant_id, revoked_by=issuer)
        print("revoked" if revoked else "not_found_or_already_revoked")
        return 0 if revoked else 1
    return 2
__all__ = [
    "DATABASE_NAME",
    "PaperOperatorTokenStore",
    "TOKEN_PREFIX",
    "authenticate_paper_operator_token",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())
