"""Offline, fail-closed SQLite order ledger primitives.

This module deliberately has no broker SDK, network, account lookup, or dispatch
code. A caller must commit :meth:`start_send` before any external send attempt;
after that point every restart is treated as uncertain until reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from src.execution.contracts import Environment, RiskGrant, canonical_json, canonical_sha256
from src.execution.contracts.models import StrictModel
from src.execution.risk import ProposedOrder, RiskDecision, RiskFacts, evaluate_order_risk


class LocalOrderState(StrEnum):
    CREATED = "CREATED"
    RESERVED = "RESERVED"
    SEND_STARTED = "SEND_STARTED"
    UNKNOWN = "UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class BrokerEventKind(StrEnum):
    ACK = "ACK"
    REJECT = "REJECT"
    FILL = "FILL"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    CANCEL_REJECTED = "CANCEL_REJECTED"


class ExecutionControlState(StrEnum):
    """Durable local send gate for one broker/account/environment scope."""

    PAUSED = "PAUSED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"


class OrderIntentInput(StrictModel):
    """Trusted service input; identity fields are supplied by the host."""

    decision_id: str = Field(min_length=1, max_length=200)
    broker: str = Field(min_length=1, max_length=100)
    account_ref: str = Field(min_length=1, max_length=200)
    environment: Environment
    deployment_id: str = Field(min_length=1, max_length=200)
    strategy_version_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_event_seq: int = Field(strict=True, gt=0)
    action_index: int = Field(strict=True, ge=0)
    bar_end: datetime
    instrument_id: str = Field(min_length=1, max_length=200)
    side: str
    integer_qty: int = Field(strict=True, gt=0)
    order_type: str = "market"
    time_in_force: str = "DAY"
    reference_price: Decimal
    eligible_at: datetime
    expires_at: datetime
    risk_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("bar_end", "eligible_at", "expires_at")
    @classmethod
    def utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("side")
    @classmethod
    def supported_side(cls, value: str) -> str:
        if value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return value

    @field_validator("reference_price")
    @classmethod
    def valid_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("reference_price must be finite and positive")
        return value

    @model_validator(mode="after")
    def supported_order_window(self) -> "OrderIntentInput":
        if self.order_type != "market" or self.time_in_force != "DAY":
            raise ValueError("only market DAY intents are supported")
        if self.expires_at <= self.eligible_at:
            raise ValueError("expires_at must be after eligible_at")
        return self


class BrokerEvent(StrictModel):
    broker: str = Field(min_length=1, max_length=100)
    account_ref: str = Field(min_length=1, max_length=200)
    environment: Environment
    intent_id: str = Field(min_length=1, max_length=200)
    broker_event_id: str = Field(min_length=1, max_length=300)
    kind: BrokerEventKind
    occurred_at: datetime
    received_at: datetime
    broker_order_id: str | None = None
    broker_exec_id: str | None = None
    quantity: int | None = Field(default=None, strict=True, gt=0)
    price: Decimal | None = None
    message: str | None = Field(default=None, max_length=2000)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("price")
    @classmethod
    def finite_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("price must be finite and positive")
        return value

    @model_validator(mode="after")
    def event_shape(self) -> "BrokerEvent":
        if self.received_at < self.occurred_at:
            raise ValueError("received_at cannot precede occurred_at")
        if self.kind is BrokerEventKind.FILL:
            if not self.broker_exec_id or self.quantity is None or self.price is None:
                raise ValueError("fill requires broker_exec_id, quantity, and price")
        elif self.broker_exec_id is not None or self.quantity is not None or self.price is not None:
            raise ValueError("only fill events may contain execution fields")
        return self


@dataclass(frozen=True, slots=True)
class OrderRecord:
    intent_id: str
    dedupe_key: str
    state: LocalOrderState
    reserved_amount: Decimal
    filled_qty: int
    remaining_qty: int
    average_fill_price: Decimal | None
    broker_order_id: str | None
    attempt_id: str | None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    ready_to_reserve: tuple[OrderRecord, ...]
    ready_to_send: tuple[OrderRecord, ...]
    unresolved_sends: tuple[OrderRecord, ...]
    active_orders: tuple[OrderRecord, ...]


class LedgerError(RuntimeError):
    """Base class for fail-closed ledger errors."""


class LedgerConflict(LedgerError):
    pass


class InvalidTransition(LedgerError):
    pass


class OrderNotFound(LedgerError):
    pass


class InvalidActivityCursor(LedgerError):
    """A caller requested an invalid or future PAPER activity cursor."""


class SQLiteOrderLedger:
    """Transactional local order/attempt/fill ledger for offline services."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = str(path)
        self.timeout_seconds = timeout_seconds
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS order_intent (
                    intent_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    body_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                      ('CREATED','RESERVED','SEND_STARTED','UNKNOWN','ACKNOWLEDGED',
                       'PARTIALLY_FILLED','FILLED','CANCEL_PENDING','CANCELLED','REJECTED')),
                    reserved_amount TEXT NOT NULL DEFAULT '0',
                    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
                    fill_notional TEXT NOT NULL DEFAULT '0',
                    broker_order_id TEXT,
                    attempt_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS order_reservation (
                    intent_id TEXT PRIMARY KEY REFERENCES order_intent(intent_id),
                    reservation_id TEXT NOT NULL UNIQUE,
                    amount TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS share_reservation (
                    intent_id TEXT PRIMARY KEY REFERENCES order_intent(intent_id),
                    reservation_id TEXT NOT NULL UNIQUE,
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    instrument_id TEXT NOT NULL,
                    remaining_qty INTEGER NOT NULL CHECK(remaining_qty > 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS submission_attempt (
                    intent_id TEXT PRIMARY KEY REFERENCES order_intent(intent_id),
                    attempt_id TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('SEND_STARTED','UNKNOWN','ACKNOWLEDGED','REJECTED'))
                );
                CREATE TABLE IF NOT EXISTS cancel_attempt (
                    intent_id TEXT PRIMARY KEY REFERENCES order_intent(intent_id),
                    cancel_id TEXT NOT NULL UNIQUE,
                    broker_order_id INTEGER NOT NULL CHECK(broker_order_id > 0),
                    requested_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('SEND_STARTED','UNKNOWN','CONFIRMED','REJECTED'))
                );
                CREATE TABLE IF NOT EXISTS broker_order_binding (
                    broker_order_id INTEGER PRIMARY KEY CHECK(broker_order_id > 0),
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    intent_id TEXT NOT NULL UNIQUE REFERENCES order_intent(intent_id),
                    bt_order_ref INTEGER NOT NULL UNIQUE CHECK(bt_order_ref > 0),
                    requested_qty INTEGER NOT NULL CHECK(requested_qty > 0),
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    contract_id INTEGER,
                    symbol TEXT,
                    security_type TEXT,
                    currency TEXT
                );
                CREATE TABLE IF NOT EXISTS broker_event (
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    broker_event_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL REFERENCES order_intent(intent_id),
                    kind TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(broker, account_ref, environment, broker_event_id)
                );
                CREATE TABLE IF NOT EXISTS fill (
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    broker_exec_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL REFERENCES order_intent(intent_id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(broker, account_ref, environment, broker_exec_id)
                );
                CREATE TABLE IF NOT EXISTS fee_fact (
                    fee_fact_id TEXT PRIMARY KEY,
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    broker_exec_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    UNIQUE(broker, account_ref, environment, broker_exec_id, evidence_sha256)
                );
                CREATE TRIGGER IF NOT EXISTS fee_fact_no_update
                BEFORE UPDATE ON fee_fact BEGIN
                    SELECT RAISE(ABORT, 'fee facts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS fee_fact_no_delete
                BEFORE DELETE ON fee_fact BEGIN
                    SELECT RAISE(ABORT, 'fee facts are append-only');
                END;
                CREATE TABLE IF NOT EXISTS execution_control (
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    state TEXT NOT NULL CHECK(state IN
                      ('PAUSED','RUNNING','STOPPING','STOPPED','RECOVERING','ERROR')),
                    reason TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(broker, account_ref, environment)
                );
                CREATE TABLE IF NOT EXISTS activity_scope_cursor (
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    last_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_seq >= 0),
                    PRIMARY KEY(broker, account_ref, environment)
                );
                CREATE TABLE IF NOT EXISTS paper_activity_event (
                    broker TEXT NOT NULL,
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    seq INTEGER NOT NULL CHECK(seq > 0),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(broker, account_ref, environment, seq)
                );
                CREATE INDEX IF NOT EXISTS paper_activity_event_scope_time
                    ON paper_activity_event(broker, account_ref, environment, seq);
                CREATE TRIGGER IF NOT EXISTS paper_activity_event_no_update
                BEFORE UPDATE ON paper_activity_event BEGIN
                    SELECT RAISE(ABORT, 'paper activity events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS paper_activity_event_no_delete
                BEFORE DELETE ON paper_activity_event BEGIN
                    SELECT RAISE(ABORT, 'paper activity events are append-only');
                END;
                """
            )
            binding_columns = {row[1] for row in connection.execute("PRAGMA table_info(broker_order_binding)")}
            for name, declaration in (
                ("contract_id", "INTEGER"), ("symbol", "TEXT"),
                ("security_type", "TEXT"), ("currency", "TEXT"),
            ):
                if name not in binding_columns:
                    connection.execute(f"ALTER TABLE broker_order_binding ADD COLUMN {name} {declaration}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._recover_legacy_sell_holds(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _append_activity_event(
        connection: sqlite3.Connection,
        *,
        broker: str,
        account_ref: str,
        environment: Environment | str,
        event_type: str,
        payload: dict,
        occurred_at: datetime | None = None,
    ) -> int:
        """Append a scoped, public activity projection in the caller's transaction.

        This helper intentionally never opens or commits a transaction. Its
        callers append only after the corresponding ledger fact has been
        persisted, so a stream cursor cannot advance independently of the
        authoritative fact.
        """
        if not broker or not account_ref or not event_type:
            raise ValueError("activity event requires scope and type")
        environment_value = Environment(environment).value
        connection.execute(
            "INSERT INTO activity_scope_cursor(broker,account_ref,environment,last_seq) "
            "VALUES(?,?,?,0) ON CONFLICT(broker,account_ref,environment) DO NOTHING",
            (broker, account_ref, environment_value),
        )
        row = connection.execute(
            "SELECT last_seq FROM activity_scope_cursor "
            "WHERE broker=? AND account_ref=? AND environment=?",
            (broker, account_ref, environment_value),
        ).fetchone()
        seq = int(row["last_seq"]) + 1
        connection.execute(
            "UPDATE activity_scope_cursor SET last_seq=? "
            "WHERE broker=? AND account_ref=? AND environment=?",
            (seq, broker, account_ref, environment_value),
        )
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("activity event timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        connection.execute(
            "INSERT INTO paper_activity_event "
            "(broker,account_ref,environment,seq,event_type,payload_json,occurred_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (broker, account_ref, environment_value, seq, event_type,
             canonical_json(payload), timestamp.isoformat()),
        )
        return seq

    @staticmethod
    def _recover_legacy_sell_holds(connection: sqlite3.Connection) -> None:
        """Freeze scopes with active legacy SELL orders lacking share holds.

        Their remaining position cannot be reconstructed safely from the old
        cash-only reservation schema. Broker reconciliation must establish the
        current shares and open-order state before dispatch resumes.
        """
        rows = connection.execute(
            "SELECT oi.intent_id,oi.body_json FROM order_intent AS oi "
            "WHERE oi.state IN ('RESERVED','SEND_STARTED','UNKNOWN','ACKNOWLEDGED',"
            "'PARTIALLY_FILLED','CANCEL_PENDING') "
            "AND NOT EXISTS (SELECT 1 FROM share_reservation sr WHERE sr.intent_id=oi.intent_id)"
        ).fetchall()
        for row in rows:
            intent = json.loads(row["body_json"])
            if intent.get("side") != "SELL":
                continue
            prior = connection.execute(
                "SELECT state,reason FROM execution_control WHERE broker=? AND account_ref=? AND environment=?",
                (intent["broker"], intent["account_ref"], intent["environment"]),
            ).fetchone()
            connection.execute(
                "INSERT INTO execution_control(broker,account_ref,environment,state,reason) "
                "VALUES(?,?,?,'RECOVERING','legacy_sell_share_reservation_missing') "
                "ON CONFLICT(broker,account_ref,environment) DO UPDATE SET "
                "state='RECOVERING',reason='legacy_sell_share_reservation_missing',"
                "updated_at=CURRENT_TIMESTAMP",
                (intent["broker"], intent["account_ref"], intent["environment"]),
            )
            if prior is None or prior["state"] != ExecutionControlState.RECOVERING.value or prior["reason"] != "legacy_sell_share_reservation_missing":
                SQLiteOrderLedger._append_activity_event(
                    connection, broker=intent["broker"], account_ref=intent["account_ref"],
                    environment=intent["environment"], event_type="control.state",
                    payload={"state": ExecutionControlState.RECOVERING.value,
                             "reason": "legacy_sell_share_reservation_missing"},
                )

    def get_execution_control(
        self, *, broker: str, account_ref: str, environment: Environment
    ) -> tuple[ExecutionControlState, str | None]:
        """Return the durable scope gate; a scope with no row starts PAUSED."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state,reason FROM execution_control "
                "WHERE broker=? AND account_ref=? AND environment=?",
                (broker, account_ref, Environment(environment).value),
            ).fetchone()
        if row is None:
            return ExecutionControlState.PAUSED, "not_activated"
        return ExecutionControlState(row["state"]), row["reason"]

    def set_execution_control(
        self,
        *,
        broker: str,
        account_ref: str,
        environment: Environment,
        state: ExecutionControlState,
        reason: str | None = None,
    ) -> None:
        """Persist a trusted host's run-control decision for one exact scope."""
        if not broker or not account_ref:
            raise ValueError("broker and account_ref are required")
        state = ExecutionControlState(state)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO execution_control(broker,account_ref,environment,state,reason) "
                "VALUES(?,?,?,?,?) ON CONFLICT(broker,account_ref,environment) DO UPDATE SET "
                "state=excluded.state,reason=excluded.reason,updated_at=CURRENT_TIMESTAMP",
                (broker, account_ref, Environment(environment).value, state.value, reason),
            )
            self._append_activity_event(
                connection, broker=broker, account_ref=account_ref,
                environment=environment, event_type="control.state",
                payload={"state": state.value, "reason": reason},
            )
            connection.commit()

    @staticmethod
    def _dedupe_key(intent: OrderIntentInput) -> str:
        # decision_id is the host-issued logical event/action identity. Keep it
        # in the key so separate runs over the same version/bar do not collide.
        scope = {
            "decision_id": intent.decision_id,
            "broker": intent.broker,
            "account_ref": intent.account_ref,
            "environment": intent.environment.value,
            "deployment_id": intent.deployment_id,
            "strategy_version_hash": intent.strategy_version_hash,
            "input_event_seq": intent.input_event_seq,
            "action_index": intent.action_index,
            "bar_end": intent.bar_end,
        }
        return hashlib.sha256(canonical_json(scope).encode("utf-8")).hexdigest()

    def create_intent(self, value: OrderIntentInput | dict) -> OrderRecord:
        intent = value if isinstance(value, OrderIntentInput) else OrderIntentInput.model_validate(value)
        dedupe_key = self._dedupe_key(intent)
        body_json = canonical_json(intent)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT intent_id, body_json FROM order_intent WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if existing:
                if existing["body_json"] != body_json:
                    connection.rollback()
                    raise LedgerConflict("logical order intent key already exists with different content")
                connection.commit()
                return self.get_order(existing["intent_id"])
            intent_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO order_intent(intent_id,dedupe_key,body_json,state) VALUES(?,?,?,'CREATED')",
                (intent_id, dedupe_key, body_json),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.created",
                payload={"intent_id": intent_id, "state": LocalOrderState.CREATED.value},
                occurred_at=intent.eligible_at,
            )
            connection.commit()
        return self.get_order(intent_id)

    def reserve(self, intent_id: str, reservation_id: str, amount: Decimal | str) -> OrderRecord:
        try:
            reserved = Decimal(amount)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("reservation amount must be a finite decimal") from exc
        if not reserved.is_finite() or reserved <= 0:
            raise ValueError("reservation amount must be finite and positive")
        if not reservation_id:
            raise ValueError("reservation_id is required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            if intent.side != "BUY":
                connection.rollback()
                raise InvalidTransition("SELL intents require a share reservation, not a cash reservation")
            if row["state"] == LocalOrderState.RESERVED:
                existing = connection.execute(
                    "SELECT reservation_id, amount FROM order_reservation WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                if existing and existing["reservation_id"] == reservation_id and Decimal(existing["amount"]) == reserved:
                    connection.commit()
                    return self.get_order(intent_id)
                connection.rollback()
                raise LedgerConflict("reservation already exists with different identity or amount")
            if row["state"] != LocalOrderState.CREATED:
                connection.rollback()
                raise InvalidTransition("only CREATED intents can be reserved")
            connection.execute(
                "INSERT INTO order_reservation(intent_id,reservation_id,amount) VALUES(?,?,?)",
                (intent_id, reservation_id, str(reserved)),
            )
            connection.execute(
                "UPDATE order_intent SET state='RESERVED', reserved_amount=?, updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (str(reserved), intent_id),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.RESERVED.value,
                         "reserved_amount": str(reserved)},
            )
            connection.commit()
        return self.get_order(intent_id)

    def reserve_sell(
        self,
        intent_id: str,
        reservation_id: str,
        *,
        available_position_qty: int,
        account_as_of: datetime,
    ) -> OrderRecord:
        """Atomically reserve strategy-owned shares for one PAPER SELL intent.

        ``available_position_qty`` must come from the trusted, fresh risk facts
        snapshot. Existing active SELL holds for the same account/instrument
        are subtracted while holding SQLite's write lock. This prevents two
        concurrent intents from reserving the same shares in this ledger.
        """
        if not reservation_id:
            raise ValueError("reservation_id is required")
        if type(available_position_qty) is not int or available_position_qty < 0:
            raise ValueError("available_position_qty must be a non-negative integer")
        if account_as_of.tzinfo is None or account_as_of.utcoffset() is None:
            raise ValueError("account_as_of must be timezone-aware")
        account_as_of = account_as_of.astimezone(timezone.utc)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            if intent.side != "SELL" or intent.environment is not Environment.PAPER:
                connection.rollback()
                raise InvalidTransition("share reservations only support PAPER SELL intents")
            if row["state"] == LocalOrderState.RESERVED:
                existing = connection.execute(
                    "SELECT reservation_id,broker,account_ref,environment,instrument_id,remaining_qty "
                    "FROM share_reservation WHERE intent_id=?", (intent_id,),
                ).fetchone()
                if existing and (
                    existing["reservation_id"] == reservation_id
                    and existing["broker"] == intent.broker
                    and existing["account_ref"] == intent.account_ref
                    and existing["environment"] == intent.environment.value
                    and existing["instrument_id"] == intent.instrument_id
                    and int(existing["remaining_qty"]) == intent.integer_qty
                ):
                    connection.commit()
                    return self.get_order(intent_id)
                connection.rollback()
                raise LedgerConflict("share reservation already exists with different identity or quantity")
            if row["state"] != LocalOrderState.CREATED:
                connection.rollback()
                raise InvalidTransition("only CREATED intents can reserve shares")
            if self._sell_fill_after_snapshot(
                connection,
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
                instrument_id=intent.instrument_id,
                account_as_of=account_as_of,
            ):
                connection.rollback()
                raise LedgerConflict("trusted position snapshot predates a committed SELL fill")
            reserved = self._reserved_sell_quantity(
                connection,
                broker=intent.broker,
                account_ref=intent.account_ref,
                environment=intent.environment,
                instrument_id=intent.instrument_id,
            )
            if reserved + intent.integer_qty > available_position_qty:
                connection.rollback()
                raise LedgerConflict("active SELL reservations exceed trusted strategy-owned shares")
            connection.execute(
                "INSERT INTO share_reservation"
                "(intent_id,reservation_id,broker,account_ref,environment,instrument_id,remaining_qty) "
                "VALUES(?,?,?,?,?,?,?)",
                (intent_id, reservation_id, intent.broker, intent.account_ref,
                 intent.environment.value, intent.instrument_id, intent.integer_qty),
            )
            connection.execute(
                "UPDATE order_intent SET state='RESERVED',reserved_amount='0',updated_at=CURRENT_TIMESTAMP "
                "WHERE intent_id=?", (intent_id,),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.RESERVED.value,
                         "reserved_shares": intent.integer_qty},
            )
            connection.commit()
        return self.get_order(intent_id)

    @staticmethod
    def _reserved_sell_quantity(
        connection: sqlite3.Connection,
        *,
        broker: str,
        account_ref: str,
        environment: Environment,
        instrument_id: str,
        excluding_intent_id: str | None = None,
    ) -> int:
        sql = (
            "SELECT COALESCE(SUM(sr.remaining_qty),0) FROM share_reservation sr "
            "JOIN order_intent oi ON oi.intent_id=sr.intent_id "
            "WHERE sr.broker=? AND sr.account_ref=? AND sr.environment=? AND sr.instrument_id=? "
            "AND oi.state IN ('RESERVED','SEND_STARTED','UNKNOWN','ACKNOWLEDGED',"
            "'PARTIALLY_FILLED','CANCEL_PENDING')"
        )
        args: list[object] = [broker, account_ref, environment.value, instrument_id]
        if excluding_intent_id is not None:
            sql += " AND sr.intent_id<>?"
            args.append(excluding_intent_id)
        row = connection.execute(sql, tuple(args)).fetchone()
        return int(row[0])

    @staticmethod
    def _sell_fill_after_snapshot(
        connection: sqlite3.Connection,
        *,
        broker: str,
        account_ref: str,
        environment: Environment,
        instrument_id: str,
        account_as_of: datetime,
    ) -> bool:
        rows = connection.execute(
            "SELECT f.occurred_at,oi.body_json FROM fill f "
            "JOIN order_intent oi ON oi.intent_id=f.intent_id "
            "WHERE f.broker=? AND f.account_ref=? AND f.environment=?",
            (broker, account_ref, environment.value),
        ).fetchall()
        for row in rows:
            intent = json.loads(row["body_json"])
            if intent.get("side") != "SELL" or intent.get("instrument_id") != instrument_id:
                continue
            occurred_at = datetime.fromisoformat(row["occurred_at"])
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                return True
            # Equal timestamps cannot establish whether the snapshot includes
            # this fill, so require a strictly newer snapshot watermark.
            if occurred_at.astimezone(timezone.utc) >= account_as_of:
                return True
        return False

    def reserved_sell_quantity(
        self,
        *,
        broker: str,
        account_ref: str,
        environment: Environment,
        instrument_id: str,
        excluding_intent_id: str | None = None,
    ) -> int:
        with closing(self._connect()) as connection:
            return self._reserved_sell_quantity(
                connection, broker=broker, account_ref=account_ref,
                environment=Environment(environment), instrument_id=instrument_id,
                excluding_intent_id=excluding_intent_id,
            )

    def sell_position_snapshot_is_current(
        self,
        *,
        broker: str,
        account_ref: str,
        environment: Environment,
        instrument_id: str,
        account_as_of: datetime,
    ) -> bool:
        if account_as_of.tzinfo is None or account_as_of.utcoffset() is None:
            raise ValueError("account_as_of must be timezone-aware")
        with closing(self._connect()) as connection:
            return not self._sell_fill_after_snapshot(
                connection,
                broker=broker,
                account_ref=account_ref,
                environment=Environment(environment),
                instrument_id=instrument_id,
                account_as_of=account_as_of.astimezone(timezone.utc),
            )

    def release_before_send(self, intent_id: str, reservation_id: str) -> OrderRecord:
        """Release a pre-send hold and return RESERVED to CREATED atomically.

        This transition is only available before a submission attempt exists.
        It cannot erase or reopen a possibly dispatched order.
        """
        if not reservation_id:
            raise ValueError("reservation_id is required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            if row["state"] != LocalOrderState.RESERVED:
                connection.rollback()
                raise InvalidTransition("only RESERVED intents can release a pre-send reservation")
            attempt = connection.execute(
                "SELECT 1 FROM submission_attempt WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if attempt is not None:
                connection.rollback()
                raise InvalidTransition("a reservation cannot be released after send has started")
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            if intent.side == "SELL":
                reservation = connection.execute(
                    "SELECT reservation_id FROM share_reservation WHERE intent_id=?", (intent_id,)
                ).fetchone()
            else:
                reservation = connection.execute(
                    "SELECT reservation_id FROM order_reservation WHERE intent_id=?", (intent_id,)
                ).fetchone()
            if reservation is None or reservation["reservation_id"] != reservation_id:
                connection.rollback()
                raise LedgerConflict("reservation identity does not match the pre-send hold")
            if intent.side == "SELL":
                connection.execute("DELETE FROM share_reservation WHERE intent_id=?", (intent_id,))
            else:
                connection.execute("DELETE FROM order_reservation WHERE intent_id=?", (intent_id,))
            connection.execute(
                "UPDATE order_intent SET state='CREATED',reserved_amount='0',updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (intent_id,),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.CREATED.value,
                         "reason": "reservation_released_before_send"},
            )
            connection.commit()
        return self.get_order(intent_id)

    def start_send(self, intent_id: str, *, attempt_id: str, client_order_id: str) -> OrderRecord:
        """Persist the unique send boundary; caller may send only after return."""
        if not attempt_id or not client_order_id:
            raise ValueError("attempt_id and client_order_id are required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            if row["state"] != LocalOrderState.RESERVED:
                connection.rollback()
                raise InvalidTransition("only RESERVED intents may start one send attempt")
            if intent.side == "SELL":
                connection.rollback()
                raise InvalidTransition("SELL intents require the final risk-checked send gate")
            connection.execute(
                "INSERT INTO submission_attempt(intent_id,attempt_id,client_order_id,started_at,state) "
                "VALUES(?,?,?,?,'SEND_STARTED')",
                (intent_id, attempt_id, client_order_id, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                "UPDATE order_intent SET state='SEND_STARTED',attempt_id=?,updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (attempt_id, intent_id),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.SEND_STARTED.value,
                         "attempt_id": attempt_id},
            )
            connection.commit()
        return self.get_order(intent_id)

    def start_send_after_risk(
        self,
        intent_id: str,
        *,
        attempt_id: str,
        client_order_id: str,
        grant: RiskGrant | dict,
        facts: RiskFacts | dict,
        deployment_id: str,
        now: datetime | Callable[[], datetime],
        broker_binding: dict | None = None,
    ) -> tuple[OrderRecord, RiskDecision]:
        """Atomically check run control, add local reservations, rerun risk, and start one send.

        The caller supplies a fresh account snapshot whose reservation totals
        exclude this SQLite ledger. All other active ledger holds for the same
        account and strategy are added while holding SQLite's write lock. This
        prevents concurrent local intents from spending the same cash snapshot.
        A non-RUNNING control state or risk denial releases this intent's
        pre-send hold and returns it to CREATED; neither creates an attempt.
        """
        if not attempt_id or not client_order_id:
            raise ValueError("attempt_id and client_order_id are required")
        grant = grant if isinstance(grant, RiskGrant) else RiskGrant.model_validate(grant)
        facts = facts if isinstance(facts, RiskFacts) else RiskFacts.model_validate(facts)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            if row["state"] != LocalOrderState.RESERVED:
                connection.rollback()
                raise InvalidTransition("only RESERVED intents may start one send attempt")
            if connection.execute(
                "SELECT 1 FROM submission_attempt WHERE intent_id=?", (intent_id,)
            ).fetchone():
                connection.rollback()
                raise InvalidTransition("submission attempt already exists")
            intent_model = OrderIntentInput.model_validate_json(row["body_json"])
            intent_data = intent_model.model_dump(mode="json")
            if intent_model.side == "SELL":
                reservation = connection.execute(
                    "SELECT reservation_id,broker,account_ref,environment,instrument_id,remaining_qty "
                    "FROM share_reservation WHERE intent_id=?", (intent_id,),
                ).fetchone()
                if reservation is None or (
                    reservation["broker"] != intent_model.broker
                    or reservation["account_ref"] != intent_model.account_ref
                    or reservation["environment"] != intent_model.environment.value
                    or reservation["instrument_id"] != intent_model.instrument_id
                    or int(reservation["remaining_qty"]) != intent_model.integer_qty - int(row["filled_qty"])
                ):
                    connection.rollback()
                    raise LedgerConflict("share reservation does not match the durable SELL intent")
            else:
                reservation = connection.execute(
                    "SELECT reservation_id,amount FROM order_reservation WHERE intent_id=?", (intent_id,)
                ).fetchone()
                if reservation is None or Decimal(reservation["amount"]) != Decimal(row["reserved_amount"]):
                    connection.rollback()
                    raise LedgerConflict("reserved amount does not match its durable reservation")
            checked_at = now() if callable(now) else now
            if checked_at.tzinfo is None or checked_at.utcoffset() is None:
                connection.rollback()
                raise ValueError("risk check time must be timezone-aware")
            checked_at = checked_at.astimezone(timezone.utc)
            policy_reasons = []
            control = connection.execute(
                "SELECT state FROM execution_control WHERE broker=? AND account_ref=? AND environment=?",
                (intent_model.broker, intent_model.account_ref, intent_model.environment.value),
            ).fetchone()
            if control is None or control["state"] != ExecutionControlState.RUNNING.value:
                policy_reasons.append("execution_not_running")
            active_without_share_hold = connection.execute(
                "SELECT oi.body_json FROM order_intent oi "
                "WHERE oi.state IN ('RESERVED','SEND_STARTED','UNKNOWN','ACKNOWLEDGED',"
                "'PARTIALLY_FILLED','CANCEL_PENDING') "
                "AND NOT EXISTS (SELECT 1 FROM share_reservation sr WHERE sr.intent_id=oi.intent_id)"
            ).fetchall()
            for active in active_without_share_hold:
                other_intent = json.loads(active["body_json"])
                if (
                    other_intent.get("side") == "SELL"
                    and other_intent.get("broker") == intent_model.broker
                    and other_intent.get("account_ref") == intent_model.account_ref
                    and other_intent.get("environment") == intent_model.environment.value
                ):
                    # An active legacy SELL without a quantity hold cannot be
                    # reconciled from the cash-only ledger. Block the whole
                    # account scope until external order/position reconciliation.
                    policy_reasons.append("legacy_sell_share_reservation_missing")
                    break
            if intent_model.environment is not Environment.PAPER:
                policy_reasons.append("paper_only")
            if checked_at < intent_model.eligible_at:
                policy_reasons.append("not_yet_eligible")
            if checked_at >= intent_model.expires_at:
                policy_reasons.append("intent_expired")
            sell_reservations = 0
            if intent_model.side == "SELL":
                sell_reservations = self._reserved_sell_quantity(
                    connection,
                    broker=intent_model.broker,
                    account_ref=intent_model.account_ref,
                    environment=intent_model.environment,
                    instrument_id=intent_model.instrument_id,
                    excluding_intent_id=intent_id,
                )
                if self._sell_fill_after_snapshot(
                    connection,
                    broker=intent_model.broker,
                    account_ref=intent_model.account_ref,
                    environment=intent_model.environment,
                    instrument_id=intent_model.instrument_id,
                    account_as_of=facts.account_as_of,
                ):
                    policy_reasons.append("position_snapshot_predates_sell_fill")
            if policy_reasons:
                reasons = tuple(sorted(set(policy_reasons)))
                decision = RiskDecision(
                    allowed=False,
                    reason_codes=reasons,
                    checked_at=checked_at,
                    risk_snapshot_id=canonical_sha256({
                        "intent_id": intent_id,
                        "checked_at": checked_at,
                        "policy_reasons": reasons,
                    }),
                    estimated_notional=Decimal("0"),
                    estimated_cost=Decimal("0"),
                    projected_strategy_exposure=Decimal("0"),
                    projected_gross_exposure=Decimal("0"),
                )
                if intent_model.side == "SELL":
                    connection.execute("DELETE FROM share_reservation WHERE intent_id=?", (intent_id,))
                else:
                    connection.execute("DELETE FROM order_reservation WHERE intent_id=?", (intent_id,))
                connection.execute(
                    "UPDATE order_intent SET state='CREATED',reserved_amount='0',updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                    (intent_id,),
                )
                self._append_activity_event(
                    connection, broker=intent_model.broker, account_ref=intent_model.account_ref,
                    environment=intent_model.environment, event_type="order.state",
                    payload={"intent_id": intent_id, "state": LocalOrderState.CREATED.value,
                             "reason": "send_gate_denied"},
                    occurred_at=checked_at,
                )
                connection.commit()
                return self.get_order(intent_id), decision

            account_reservations = Decimal("0")
            strategy_reservations = Decimal("0")
            active_rows = connection.execute(
                "SELECT body_json,reserved_amount FROM order_intent "
                "WHERE state IN ('RESERVED','SEND_STARTED','UNKNOWN','ACKNOWLEDGED',"
                "'PARTIALLY_FILLED','CANCEL_PENDING') AND intent_id<>?",
                (intent_id,),
            ).fetchall()
            for active in active_rows:
                other = json.loads(active["body_json"])
                if (
                    other["broker"] == intent_data["broker"]
                    and other["account_ref"] == intent_data["account_ref"]
                    and other["environment"] == intent_data["environment"]
                ):
                    held = Decimal(active["reserved_amount"])
                    account_reservations += held
                    if (
                        other["deployment_id"] == intent_data["deployment_id"]
                        and other["strategy_version_hash"] == intent_data["strategy_version_hash"]
                    ):
                        strategy_reservations += held

            effective_facts = facts.model_copy(
                update={
                    "now": checked_at,
                    "account_buy_reservations": facts.account_buy_reservations + account_reservations,
                    "strategy_buy_reservations": facts.strategy_buy_reservations + strategy_reservations,
                    "strategy_sell_reservations": facts.strategy_sell_reservations + sell_reservations,
                }
            )
            candidate = ProposedOrder(
                instrument_id=intent_data["instrument_id"],
                side=intent_data["side"],
                integer_qty=intent_data["integer_qty"],
                order_type=intent_data["order_type"],
                time_in_force=intent_data["time_in_force"],
            )
            decision = evaluate_order_risk(grant, candidate, effective_facts)
            identity_reasons = []
            if deployment_id != intent_data["deployment_id"]:
                identity_reasons.append("deployment_mismatch")
            if grant.broker != intent_data["broker"] or facts.broker != intent_data["broker"]:
                identity_reasons.append("broker_mismatch")
            if grant.account_ref != intent_data["account_ref"] or facts.account_ref != intent_data["account_ref"]:
                identity_reasons.append("account_mismatch")
            if grant.environment.value != intent_data["environment"] or facts.environment.value != intent_data["environment"]:
                identity_reasons.append("environment_mismatch")
            if (
                grant.strategy_version_hash != intent_data["strategy_version_hash"]
                or facts.strategy_version_hash != intent_data["strategy_version_hash"]
            ):
                identity_reasons.append("strategy_version_mismatch")
            if facts.instrument_id != intent_data["instrument_id"]:
                identity_reasons.append("instrument_mismatch")
            if identity_reasons:
                reasons = tuple(sorted(set(decision.reason_codes).union(identity_reasons)))
                decision = decision.model_copy(
                    update={
                        "allowed": False,
                        "reason_codes": reasons,
                        "risk_snapshot_id": canonical_sha256({
                            "base_risk_snapshot_id": decision.risk_snapshot_id,
                            "intent_id": intent_id,
                            "deployment_id": deployment_id,
                            "identity_mismatch_reasons": reasons,
                        }),
                    }
                )
            if (
                intent_model.side == "BUY"
                and decision.allowed
                and decision.estimated_cost > Decimal(reservation["amount"])
            ):
                reasons = tuple(sorted(set(decision.reason_codes).union({"reservation_insufficient"})))
                decision = decision.model_copy(
                    update={
                        "allowed": False,
                        "reason_codes": reasons,
                        "risk_snapshot_id": canonical_sha256({
                            "base_risk_snapshot_id": decision.risk_snapshot_id,
                            "reservation_id": reservation["reservation_id"],
                            "reserved_amount": reservation["amount"],
                            "required_amount": decision.estimated_cost,
                            "reason_codes": reasons,
                        }),
                    }
                )
            if not decision.allowed:
                if intent_model.side == "SELL":
                    connection.execute("DELETE FROM share_reservation WHERE intent_id=?", (intent_id,))
                else:
                    connection.execute("DELETE FROM order_reservation WHERE intent_id=?", (intent_id,))
                connection.execute(
                    "UPDATE order_intent SET state='CREATED',reserved_amount='0',updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                    (intent_id,),
                )
                self._append_activity_event(
                    connection, broker=intent_model.broker, account_ref=intent_model.account_ref,
                    environment=intent_model.environment, event_type="order.state",
                    payload={"intent_id": intent_id, "state": LocalOrderState.CREATED.value,
                             "reason": "risk_denied"},
                    occurred_at=checked_at,
                )
                connection.commit()
                return self.get_order(intent_id), decision

            if broker_binding is not None:
                expected_binding = {
                    "account_ref": intent_data["account_ref"],
                    "environment": intent_data["environment"],
                    "intent_id": intent_id,
                    "requested_qty": intent_data["integer_qty"],
                    "side": intent_data["side"],
                }
                for key, expected in expected_binding.items():
                    if broker_binding.get(key) != expected:
                        connection.rollback()
                        raise LedgerConflict(f"broker binding {key} differs from authorized intent")
                contract_id = broker_binding.get("contract_id")
                if type(contract_id) is not int or contract_id <= 0 or any(
                    not isinstance(broker_binding.get(key), str) or not broker_binding[key]
                    for key in ("symbol", "security_type", "currency")
                ):
                    connection.rollback()
                    raise ValueError("broker binding requires verified contract identity")
                if broker_binding["symbol"] != intent_data["instrument_id"]:
                    connection.rollback()
                    raise LedgerConflict("broker binding symbol differs from authorized instrument")
                for key in ("broker_order_id", "bt_order_ref"):
                    value = broker_binding.get(key)
                    if type(value) is not int or value <= 0:
                        connection.rollback()
                        raise ValueError(f"broker binding {key} must be a positive integer")
                connection.execute(
                    "INSERT INTO broker_order_binding "
                    "(broker_order_id,account_ref,environment,intent_id,bt_order_ref,requested_qty,side,"
                    "contract_id,symbol,security_type,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        broker_binding["broker_order_id"], expected_binding["account_ref"],
                        expected_binding["environment"], intent_id, broker_binding["bt_order_ref"],
                        expected_binding["requested_qty"], expected_binding["side"], contract_id,
                        broker_binding["symbol"], broker_binding["security_type"], broker_binding["currency"],
                    ),
                )

            connection.execute(
                "INSERT INTO submission_attempt(intent_id,attempt_id,client_order_id,started_at,state) "
                "VALUES(?,?,?,?, 'SEND_STARTED')",
                (intent_id, attempt_id, client_order_id, checked_at.isoformat()),
            )
            connection.execute(
                "UPDATE order_intent SET state='SEND_STARTED',attempt_id=?,updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (attempt_id, intent_id),
            )
            self._append_activity_event(
                connection, broker=intent_model.broker, account_ref=intent_model.account_ref,
                environment=intent_model.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.SEND_STARTED.value,
                         "attempt_id": attempt_id},
                occurred_at=checked_at,
            )
            connection.commit()
        return self.get_order(intent_id), decision

    def mark_unknown(self, intent_id: str) -> OrderRecord:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            if row["state"] not in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}:
                connection.rollback()
                raise InvalidTransition("only a started send can become UNKNOWN")
            connection.execute(
                "UPDATE order_intent SET state='UNKNOWN',updated_at=CURRENT_TIMESTAMP WHERE intent_id=?", (intent_id,)
            )
            connection.execute(
                "UPDATE submission_attempt SET state='UNKNOWN' WHERE intent_id=?", (intent_id,)
            )
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.UNKNOWN.value},
            )
            connection.commit()
        return self.get_order(intent_id)

    def start_cancel(self, intent_id: str, *, cancel_id: str, now: datetime) -> int:
        """Durably claim the one cancellation attempt for an owned broker order.

        The method only accepts an acknowledged/partially filled PAPER order
        with an exact durable Backtrader binding. A successful return is the
        boundary after which a caller may invoke the broker cancel API.
        """
        if not isinstance(cancel_id, str) or not cancel_id:
            raise ValueError("cancel_id is required")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("cancel request time must be timezone-aware")
        now = now.astimezone(timezone.utc)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, intent_id)
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            state = LocalOrderState(row["state"])
            if intent.environment is not Environment.PAPER:
                connection.rollback()
                raise InvalidTransition("only PAPER orders may use this cancellation path")
            if connection.execute(
                "SELECT 1 FROM cancel_attempt WHERE intent_id=? OR cancel_id=?",
                (intent_id, cancel_id),
            ).fetchone():
                connection.rollback()
                raise InvalidTransition("a cancel attempt already exists; reconcile before any retry")
            if state not in {LocalOrderState.ACKNOWLEDGED, LocalOrderState.PARTIALLY_FILLED}:
                connection.rollback()
                raise InvalidTransition("only acknowledged active orders may be cancelled")
            binding = connection.execute(
                "SELECT broker_order_id,account_ref,environment,intent_id,requested_qty,side "
                "FROM broker_order_binding WHERE intent_id=?", (intent_id,),
            ).fetchone()
            if binding is None or (
                binding[1] != intent.account_ref or binding[2] != intent.environment.value
                or binding[3] != intent_id or binding[4] != intent.integer_qty or binding[5] != intent.side
            ):
                connection.rollback()
                raise LedgerConflict("cancel requires an exact durable broker-order binding")
            if row["broker_order_id"] not in (None, str(binding[0])):
                connection.rollback()
                raise LedgerConflict("cancel broker order ID differs from the durable binding")
            connection.execute(
                "INSERT INTO cancel_attempt(intent_id,cancel_id,broker_order_id,requested_at,state) "
                "VALUES(?,?,?,?, 'SEND_STARTED')",
                (intent_id, cancel_id, binding[0], now.isoformat()),
            )
            connection.execute(
                "UPDATE order_intent SET state='CANCEL_PENDING',updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (intent_id,),
            )
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="order.state",
                payload={"intent_id": intent_id, "state": LocalOrderState.CANCEL_PENDING.value,
                         "cancel_id": cancel_id}, occurred_at=now,
            )
            connection.commit()
        return int(binding[0])

    def mark_cancel_unknown(self, intent_id: str) -> None:
        """Freeze a possibly transmitted cancellation; it is never auto-retried."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM cancel_attempt WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None or row["state"] not in {"SEND_STARTED", "UNKNOWN"}:
                connection.rollback()
                raise InvalidTransition("only a started cancel can become UNKNOWN")
            connection.execute(
                "UPDATE cancel_attempt SET state='UNKNOWN' WHERE intent_id=?", (intent_id,)
            )
            intent = OrderIntentInput.model_validate_json(self._get_row(connection, intent_id)["body_json"])
            self._append_activity_event(
                connection, broker=intent.broker, account_ref=intent.account_ref,
                environment=intent.environment, event_type="cancel.state",
                payload={"intent_id": intent_id, "state": "UNKNOWN"},
            )
            connection.commit()

    def record_broker_event(self, value: BrokerEvent | dict) -> bool:
        """Apply one broker report; exact duplicates return False.

        Event identity and fill execution identity are independently unique.
        Contradictory reuse of either ID raises and leaves the database intact.
        """
        event = value if isinstance(value, BrokerEvent) else BrokerEvent.model_validate(value)
        body_json = canonical_json(event)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = self._get_row(connection, event.intent_id)
            intent = json.loads(order["body_json"])
            if (intent["account_ref"] != event.account_ref or intent["environment"] != event.environment.value):
                connection.rollback()
                raise LedgerConflict("broker event account or environment does not match the intent")
            if intent["broker"] != event.broker:
                connection.rollback()
                raise LedgerConflict("broker event broker does not match the intent")
            attempt = connection.execute(
                "SELECT 1 FROM submission_attempt WHERE intent_id=?", (event.intent_id,)
            ).fetchone()
            if attempt is None:
                connection.rollback()
                raise InvalidTransition("broker report cannot precede the persisted send attempt")
            prior = connection.execute(
                "SELECT body_json FROM broker_event WHERE broker=? AND account_ref=? AND environment=? AND broker_event_id=?",
                (event.broker, event.account_ref, event.environment.value, event.broker_event_id),
            ).fetchone()
            if prior:
                if prior["body_json"] == body_json:
                    connection.rollback()
                    return False
                connection.rollback()
                raise LedgerConflict("broker event ID was reused with different content")
            if event.kind is BrokerEventKind.FILL:
                if event.broker_order_id and order["broker_order_id"] not in (None, event.broker_order_id):
                    connection.rollback()
                    raise LedgerConflict("broker order ID changed for one intent")
                prior_fill = connection.execute(
                    "SELECT intent_id, quantity, price FROM fill WHERE broker=? AND account_ref=? AND environment=? AND broker_exec_id=?",
                    (event.broker, event.account_ref, event.environment.value, event.broker_exec_id),
                ).fetchone()
                if prior_fill:
                    same = (
                        prior_fill["intent_id"] == event.intent_id
                        and int(prior_fill["quantity"]) == event.quantity
                        and Decimal(prior_fill["price"]) == event.price
                    )
                    if same:
                        connection.execute(
                            "INSERT INTO broker_event VALUES(?,?,?,?,?,?,?,?)",
                            (event.broker, event.account_ref, event.environment.value, event.broker_event_id,
                             event.intent_id, event.kind.value, body_json, event.received_at.isoformat()),
                        )
                        self._append_activity_event(
                            connection, broker=event.broker, account_ref=event.account_ref,
                            environment=event.environment, event_type="broker.report",
                            payload={"intent_id": event.intent_id, "broker_event_id": event.broker_event_id,
                                     "kind": event.kind.value, "duplicate_execution": True},
                            occurred_at=event.received_at,
                        )
                        connection.commit()
                        return False
                    connection.rollback()
                    raise LedgerConflict("broker execution ID was reused with different fill content")
            connection.execute(
                "INSERT INTO broker_event VALUES(?,?,?,?,?,?,?,?)",
                (event.broker, event.account_ref, event.environment.value, event.broker_event_id,
                 event.intent_id, event.kind.value, body_json, event.received_at.isoformat()),
            )
            state = LocalOrderState(order["state"])
            if event.kind is BrokerEventKind.FILL:
                connection.execute(
                    "INSERT INTO fill VALUES(?,?,?,?,?,?,?,?)",
                    (event.broker, event.account_ref, event.environment.value, event.broker_exec_id,
                     event.intent_id, event.quantity, str(event.price), event.occurred_at.isoformat()),
                )
                filled_qty = int(order["filled_qty"]) + int(event.quantity)
                intent_qty = int(intent["integer_qty"])
                if filled_qty > intent_qty:
                    connection.rollback()
                    raise LedgerConflict("cumulative fills exceed the authorized intent quantity")
                if intent["side"] == "SELL" and state not in {
                    LocalOrderState.CANCELLED, LocalOrderState.REJECTED,
                }:
                    share_hold = connection.execute(
                        "SELECT remaining_qty FROM share_reservation WHERE intent_id=?",
                        (event.intent_id,),
                    ).fetchone()
                    if share_hold is None:
                        # Persist callback facts, but close the scope gate when
                        # an active SELL has no explainable share reservation.
                        connection.execute(
                            "INSERT INTO execution_control(broker,account_ref,environment,state,reason) "
                            "VALUES(?,?,?,'RECOVERING','active_sell_share_reservation_missing') "
                            "ON CONFLICT(broker,account_ref,environment) DO UPDATE SET "
                            "state='RECOVERING',reason='active_sell_share_reservation_missing',"
                            "updated_at=CURRENT_TIMESTAMP",
                            (event.broker, event.account_ref, event.environment.value),
                        )
                        self._append_activity_event(
                            connection, broker=event.broker, account_ref=event.account_ref,
                            environment=event.environment, event_type="control.state",
                            payload={"state": ExecutionControlState.RECOVERING.value,
                                     "reason": "active_sell_share_reservation_missing"},
                            occurred_at=event.received_at,
                        )
                    else:
                        remaining_shares = int(share_hold["remaining_qty"])
                        if event.quantity > remaining_shares:
                            connection.rollback()
                            raise LedgerConflict("SELL fill exceeds its remaining share reservation")
                        new_remaining_shares = remaining_shares - event.quantity
                        if new_remaining_shares == 0:
                            connection.execute(
                                "DELETE FROM share_reservation WHERE intent_id=?", (event.intent_id,)
                            )
                        else:
                            connection.execute(
                                "UPDATE share_reservation SET remaining_qty=? WHERE intent_id=?",
                                (new_remaining_shares, event.intent_id),
                            )
                fill_notional = Decimal(order["fill_notional"]) + Decimal(event.quantity) * event.price
                original = connection.execute(
                    "SELECT amount FROM order_reservation WHERE intent_id=?", (event.intent_id,)
                ).fetchone()
                initial_reservation = Decimal(original["amount"]) if original else Decimal("0")
                if state in {LocalOrderState.CANCELLED, LocalOrderState.REJECTED}:
                    # Terminal confirmation releases the unfilled remainder.
                    # Late executions update fills but cannot recreate a hold.
                    reserved_after_fill = Decimal("0")
                    # A fill after a terminal cancel/reject changes the actual
                    # account position after the remaining reservation was
                    # released. Freeze new dispatches in the same transaction
                    # that records the execution so no subsequent send can use
                    # stale exposure facts.
                    connection.execute(
                        "INSERT INTO execution_control(broker,account_ref,environment,state,reason) "
                        "VALUES(?,?,?,'RECOVERING','late_fill_after_terminal_order') "
                        "ON CONFLICT(broker,account_ref,environment) DO UPDATE SET "
                        "state='RECOVERING',reason='late_fill_after_terminal_order',updated_at=CURRENT_TIMESTAMP",
                        (event.broker, event.account_ref, event.environment.value),
                    )
                    self._append_activity_event(
                        connection, broker=event.broker, account_ref=event.account_ref,
                        environment=event.environment, event_type="control.state",
                        payload={"state": ExecutionControlState.RECOVERING.value,
                                 "reason": "late_fill_after_terminal_order"},
                        occurred_at=event.received_at,
                    )
                else:
                    reserved_after_fill = initial_reservation * Decimal(intent_qty - filled_qty) / Decimal(intent_qty)
                # A cancellation/rejection remains visible after a late fill;
                # fill facts still accumulate and are never discarded.
                if state not in {LocalOrderState.CANCELLED, LocalOrderState.REJECTED}:
                    state = LocalOrderState.FILLED if filled_qty == intent_qty else LocalOrderState.PARTIALLY_FILLED
                connection.execute(
                    "UPDATE order_intent SET state=?,filled_qty=?,fill_notional=?,reserved_amount=?,updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                    (state.value, filled_qty, str(fill_notional), str(reserved_after_fill), event.intent_id),
                )
                if event.broker_order_id:
                    connection.execute(
                        "UPDATE order_intent SET broker_order_id=? WHERE intent_id=?",
                        (event.broker_order_id, event.intent_id),
                    )
            else:
                next_state = {
                    BrokerEventKind.ACK: LocalOrderState.ACKNOWLEDGED,
                    BrokerEventKind.REJECT: LocalOrderState.REJECTED,
                    BrokerEventKind.CANCEL_PENDING: LocalOrderState.CANCEL_PENDING,
                    BrokerEventKind.CANCELLED: LocalOrderState.CANCELLED,
                    BrokerEventKind.CANCEL_REJECTED: LocalOrderState.ACKNOWLEDGED,
                }[event.kind]
                # Never regress fill facts or overwrite a terminal cancellation
                # with a delayed acknowledgment. Contradictions require review.
                if state in {LocalOrderState.CANCELLED, LocalOrderState.REJECTED, LocalOrderState.FILLED}:
                    if event.kind is BrokerEventKind.CANCEL_REJECTED:
                        connection.rollback()
                        raise LedgerConflict("cancel rejection conflicts with a terminal order state")
                    if next_state not in {state, LocalOrderState.ACKNOWLEDGED}:
                        connection.rollback()
                        raise LedgerConflict("broker report conflicts with terminal order state")
                    # Delayed acknowledgments cannot resurrect a terminal order.
                elif state is LocalOrderState.CANCEL_PENDING and event.kind is BrokerEventKind.ACK:
                    # A delayed Submitted/PreSubmitted report does not prove
                    # that the outstanding cancel was rejected.
                    state = LocalOrderState.CANCEL_PENDING
                elif next_state is LocalOrderState.ACKNOWLEDGED and int(order["filled_qty"]) > 0:
                    state = LocalOrderState.PARTIALLY_FILLED
                else:
                    state = next_state
                if event.broker_order_id:
                    if order["broker_order_id"] not in (None, event.broker_order_id):
                        connection.rollback()
                        raise LedgerConflict("broker order ID changed for one intent")
                    connection.execute(
                        "UPDATE order_intent SET broker_order_id=? WHERE intent_id=?",
                        (event.broker_order_id, event.intent_id),
                    )
                connection.execute(
                    "UPDATE order_intent SET state=?,updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                    (state.value, event.intent_id),
                )
                attempt_state = "ACKNOWLEDGED" if event.kind in {BrokerEventKind.ACK, BrokerEventKind.CANCEL_REJECTED} else (
                    "REJECTED" if event.kind is BrokerEventKind.REJECT else order["state"]
                )
                if attempt_state in {"ACKNOWLEDGED", "REJECTED"}:
                    connection.execute("UPDATE submission_attempt SET state=? WHERE intent_id=?", (attempt_state, event.intent_id))
                if event.kind in {BrokerEventKind.REJECT, BrokerEventKind.CANCELLED}:
                    connection.execute("UPDATE order_intent SET reserved_amount='0' WHERE intent_id=?", (event.intent_id,))
                    if intent["side"] == "SELL":
                        connection.execute("DELETE FROM share_reservation WHERE intent_id=?", (event.intent_id,))
                if event.kind is BrokerEventKind.CANCELLED:
                    connection.execute(
                        "UPDATE cancel_attempt SET state='CONFIRMED' WHERE intent_id=? "
                        "AND state IN ('SEND_STARTED','UNKNOWN')", (event.intent_id,),
                    )
                elif event.kind is BrokerEventKind.CANCEL_REJECTED:
                    connection.execute(
                        "UPDATE cancel_attempt SET state='REJECTED' WHERE intent_id=? "
                        "AND state IN ('SEND_STARTED','UNKNOWN')", (event.intent_id,),
                    )
            updated = self._get_row(connection, event.intent_id)
            activity_payload = {
                "intent_id": event.intent_id,
                "broker_event_id": event.broker_event_id,
                "kind": event.kind.value,
                "state": updated["state"],
            }
            if event.kind is BrokerEventKind.FILL:
                activity_payload.update({
                    "broker_exec_id": event.broker_exec_id,
                    "quantity": event.quantity,
                    "price": str(event.price),
                    "filled_qty": int(updated["filled_qty"]),
                })
            self._append_activity_event(
                connection, broker=event.broker, account_ref=event.account_ref,
                environment=event.environment, event_type="broker.report",
                payload=activity_payload, occurred_at=event.received_at,
            )
            connection.commit()
        return True

    def append_fee_fact(
        self, *, broker: str, account_ref: str, environment: Environment | str,
        broker_exec_id: str, amount: Decimal | str, currency: str,
        received_at: datetime, evidence: dict,
    ) -> bool:
        """Append an IBKR-reported fee observation; never designate it final.

        Exact evidence replays are idempotent. A changed report for the same
        execution is retained as another immutable fact; consumers must review
        all revisions instead of replacing an earlier amount.
        """
        environment = Environment(environment)
        amount = Decimal(str(amount))
        if not amount.is_finite():
            raise ValueError("fee amount must be finite")
        if not broker or not account_ref or not broker_exec_id or not currency:
            raise ValueError("fee fact requires broker, account, execution id, and currency")
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("fee fact timestamp must be timezone aware")
        received_at = received_at.astimezone(timezone.utc)
        evidence_json = canonical_json(evidence)
        evidence_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        fact_id = "ib-fee:" + hashlib.sha256(
            canonical_json({"broker": broker, "account_ref": account_ref,
                            "environment": environment.value, "exec_id": broker_exec_id,
                            "evidence_sha256": evidence_sha256}).encode("utf-8")
        ).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT evidence_sha256 FROM fee_fact WHERE broker=? AND account_ref=? "
                "AND environment=? AND broker_exec_id=? AND evidence_sha256=?",
                (broker, account_ref, environment.value, broker_exec_id, evidence_sha256),
            ).fetchone()
            if prior:
                connection.rollback()
                return False
            connection.execute(
                "INSERT INTO fee_fact VALUES(?,?,?,?,?,?,?,?,?,?)",
                (fact_id, broker, account_ref, environment.value, broker_exec_id,
                 str(amount), currency, received_at.isoformat(),
                 evidence_json, evidence_sha256),
            )
            self._append_activity_event(
                connection, broker=broker, account_ref=account_ref,
                environment=environment, event_type="fee.reported",
                payload={"fee_fact_id": fact_id, "broker_exec_id": broker_exec_id,
                         "amount": str(amount), "currency": currency,
                         "amount_status": "REPORTED_UNCONFIRMED"},
                occurred_at=received_at,
            )
            connection.commit()
        return True

    def list_fee_facts(self, *, broker: str | None = None, account_ref: str | None = None,
                       environment: Environment | str | None = None,
                       broker_exec_id: str | None = None) -> list[dict]:
        """Read immutable fee observations with derived link/review labels.

        ``reported_unconfirmed`` is deliberately the only amount status. A
        matching fill links the fact but does not certify broker finality.
        """
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("broker", broker), ("account_ref", account_ref),
                              ("environment", Environment(environment).value if environment is not None else None),
                              ("broker_exec_id", broker_exec_id)):
            if value is not None:
                clauses.append(f"f.{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT f.*, x.intent_id AS linked_intent_id, "
                "(SELECT COUNT(*) FROM fee_fact r WHERE r.broker=f.broker "
                "AND r.account_ref=f.account_ref AND r.environment=f.environment "
                "AND r.broker_exec_id=f.broker_exec_id) AS revision_count "
                "FROM fee_fact f LEFT JOIN fill x ON x.broker=f.broker "
                "AND x.account_ref=f.account_ref AND x.environment=f.environment "
                "AND x.broker_exec_id=f.broker_exec_id" + where + " ORDER BY f.rowid", params
            ).fetchall()
        facts = []
        for row in rows:
            linked = row["linked_intent_id"] is not None
            revised = int(row["revision_count"]) > 1
            facts.append({
                "fee_fact_id": row["fee_fact_id"], "broker": row["broker"],
                "account_ref": row["account_ref"], "environment": row["environment"],
                "broker_exec_id": row["broker_exec_id"], "amount": row["amount"],
                "currency": row["currency"], "received_at": row["received_at"],
                "evidence": json.loads(row["evidence_json"]),
                "evidence_sha256": row["evidence_sha256"],
                "intent_id": row["linked_intent_id"],
                "association_status": "LINKED" if linked else "PENDING_EXECUTION",
                "review_status": "PENDING_REVIEW" if revised else "NO_REVISION_SEEN",
                "amount_status": "REPORTED_UNCONFIRMED",
            })
        return facts

    def get_order(self, intent_id: str) -> OrderRecord:
        with closing(self._connect()) as connection:
            row = self._get_row(connection, intent_id)
            return self._record(row)

    def get_intent(self, intent_id: str) -> OrderIntentInput:
        """Return the trusted, immutable intent bound to a durable intent ID."""
        with closing(self._connect()) as connection:
            row = self._get_row(connection, intent_id)
            return OrderIntentInput.model_validate_json(row["body_json"])

    def list_activity_for_scope(
        self, *, broker: str, account_ref: str, environment: Environment | str,
        limit: int = 50,
    ) -> list[dict]:
        """Return bounded order history, fills, and reported fees for one scope.

        The account/environment scope must come from the trusted runtime owner,
        never from an API request. Terminal orders are intentionally included;
        ``recovery_snapshot`` is a work queue and is not a history API.
        """
        environment = Environment(environment)
        if not broker or not account_ref:
            raise ValueError("broker and account_ref are required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM order_intent "
                "WHERE json_extract(body_json, '$.broker')=? "
                "AND json_extract(body_json, '$.account_ref')=? "
                "AND json_extract(body_json, '$.environment')=? "
                "ORDER BY created_at DESC, intent_id DESC LIMIT ?",
                (broker, account_ref, environment.value, limit),
            ).fetchall()

        activity: list[dict] = []
        for row in rows:
            intent = OrderIntentInput.model_validate_json(row["body_json"])
            if (
                intent.broker != broker
                or intent.account_ref != account_ref
                or intent.environment is not environment
            ):
                raise LedgerConflict("scoped activity query returned an intent outside its scope")
            order = self._record(row)
            fills = self.list_fills(order.intent_id)
            fee_facts: list[dict] = []
            for fill in fills:
                fee_facts.extend(self.list_fee_facts(
                    broker=broker,
                    account_ref=account_ref,
                    environment=environment,
                    broker_exec_id=str(fill["broker_exec_id"]),
                ))
            public_fills = [
                {key: value for key, value in fill.items() if key not in {"broker", "account_ref", "environment"}}
                for fill in fills
            ]
            public_fees = [{
                key: fact[key] for key in (
                    "fee_fact_id", "broker_exec_id", "amount", "currency", "received_at",
                    "evidence_sha256", "intent_id", "association_status", "review_status",
                    "amount_status",
                )
            } for fact in fee_facts]
            # Keep the trusted account identity out of the per-order body. The
            # response's trusted scope is supplied by the manager separately.
            intent_data = intent.model_dump(mode="json")
            intent_data.pop("account_ref", None)
            activity.append({
                "intent_id": order.intent_id,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "state": order.state.value,
                "reserved_amount": str(order.reserved_amount),
                "filled_qty": order.filled_qty,
                "remaining_qty": order.remaining_qty,
                "average_fill_price": (
                    str(order.average_fill_price) if order.average_fill_price is not None else None
                ),
                "broker_order_id": order.broker_order_id,
                "attempt_id": order.attempt_id,
                "intent": intent_data,
                "fills": public_fills,
                "fees": public_fees,
            })
        return activity

    def latest_activity_seq_for_scope(
        self, *, broker: str, account_ref: str, environment: Environment | str,
    ) -> int:
        environment = Environment(environment)
        if not broker or not account_ref:
            raise ValueError("broker and account_ref are required")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT last_seq FROM activity_scope_cursor "
                "WHERE broker=? AND account_ref=? AND environment=?",
                (broker, account_ref, environment.value),
            ).fetchone()
        return int(row["last_seq"]) if row is not None else 0

    def read_activity_events_for_scope(
        self,
        *,
        broker: str,
        account_ref: str,
        environment: Environment | str,
        after_seq: int,
        limit: int = 100,
    ) -> list[dict]:
        """Read durable activity after a scope-local cursor, in sequence order.

        The scope is supplied by the trusted runtime manager. Cursors that are
        malformed or ahead of the durable stream fail closed rather than
        silently skipping future events.
        """
        environment = Environment(environment)
        if not broker or not account_ref:
            raise ValueError("broker and account_ref are required")
        if type(after_seq) is not int or after_seq < 0:
            raise InvalidActivityCursor("activity cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("activity limit must be an integer between 1 and 100")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "SELECT last_seq FROM activity_scope_cursor "
                "WHERE broker=? AND account_ref=? AND environment=?",
                (broker, account_ref, environment.value),
            ).fetchone()
            latest = int(cursor["last_seq"]) if cursor is not None else 0
            if after_seq > latest:
                raise InvalidActivityCursor("activity cursor is ahead of the durable stream")
            rows = connection.execute(
                "SELECT seq,event_type,payload_json,occurred_at FROM paper_activity_event "
                "WHERE broker=? AND account_ref=? AND environment=? AND seq>? "
                "ORDER BY seq ASC LIMIT ?",
                (broker, account_ref, environment.value, after_seq, limit),
            ).fetchall()
        events = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict) or "account_ref" in payload:
                raise LedgerConflict("persisted activity payload is invalid or contains scope identity")
            events.append({
                "seq": int(row["seq"]),
                "event_type": row["event_type"],
                "payload": payload,
                "occurred_at": row["occurred_at"],
            })
        return events

    def recovery_snapshot(self) -> RecoverySnapshot:
        """Return durable work queues; UNKNOWN/SEND_STARTED are never sendable."""
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM order_intent ORDER BY created_at,intent_id").fetchall()
        records = tuple(self._record(row) for row in rows)
        return RecoverySnapshot(
            ready_to_reserve=tuple(r for r in records if r.state is LocalOrderState.CREATED),
            ready_to_send=tuple(r for r in records if r.state is LocalOrderState.RESERVED),
            unresolved_sends=tuple(r for r in records if r.state in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}),
            active_orders=tuple(
                r for r in records if r.state in {
                    LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN, LocalOrderState.ACKNOWLEDGED,
                    LocalOrderState.PARTIALLY_FILLED, LocalOrderState.CANCEL_PENDING,
                }
            ),
        )

    def recovery_snapshot_for_scope(
        self, *, broker: str, account_ref: str, environment: Environment
    ) -> RecoverySnapshot:
        """Return durable work queues for one exact broker/account scope."""
        snapshot = self.recovery_snapshot()
        selected: list[tuple[OrderRecord, ...]] = []
        for records in (
            snapshot.ready_to_reserve,
            snapshot.ready_to_send,
            snapshot.unresolved_sends,
            snapshot.active_orders,
        ):
            matching = []
            for record in records:
                intent = self.get_intent(record.intent_id)
                if (
                    intent.broker == broker
                    and intent.account_ref == account_ref
                    and intent.environment is Environment(environment)
                ):
                    matching.append(record)
            selected.append(tuple(matching))
        return RecoverySnapshot(*selected)

    def list_fills(self, intent_id: str) -> list[dict[str, str | int]]:
        with closing(self._connect()) as connection:
            self._get_row(connection, intent_id)
            rows = connection.execute(
                "SELECT broker,account_ref,environment,broker_exec_id,quantity,price,occurred_at FROM fill "
                "WHERE intent_id=? ORDER BY occurred_at,broker_exec_id", (intent_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _get_row(connection: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM order_intent WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise OrderNotFound("order intent does not exist")
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> OrderRecord:
        intent = json.loads(row["body_json"])
        filled = int(row["filled_qty"])
        notional = Decimal(row["fill_notional"])
        return OrderRecord(
            intent_id=row["intent_id"],
            dedupe_key=row["dedupe_key"],
            state=LocalOrderState(row["state"]),
            reserved_amount=Decimal(row["reserved_amount"]),
            filled_qty=filled,
            remaining_qty=max(0, int(intent["integer_qty"]) - filled),
            average_fill_price=(notional / filled if filled else None),
            broker_order_id=row["broker_order_id"],
            attempt_id=row["attempt_id"],
        )
