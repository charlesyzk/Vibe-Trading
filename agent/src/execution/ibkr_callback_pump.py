"""Fail-closed offline bridge from normalized IBKR callbacks to the ledger and BT.

The official SDK callback thread only enqueues primitive ``CallbackEvent``
values. This trusted pump validates the configured account and a durable order
binding, persists a replayable inbox item, commits the broker fact to the
order ledger, then hands cumulative state to ``PortBroker``. It never connects
to IBKR or calls the SDK.

Commission reports are retained as immutable, execution-keyed observations.
Changed reports remain separate revisions flagged for review; they never
replace an earlier amount or claim broker finality.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.bt_runtime.brokers.proxy import BrokerCallback, CallbackStatus
from src.execution.contracts import Environment, canonical_json
from src.execution.order_ledger import (
    BrokerEvent,
    BrokerEventKind,
    LedgerConflict,
    LocalOrderState,
    SQLiteOrderLedger,
)


class CallbackPumpFatal(RuntimeError):
    """A callback cannot be safely attributed or applied; pause execution."""


# IBKR documents these exact error() codes as connection/farm status notices,
# including several that are emitted during an ordinary successful handshake.
# Keep this allowlist deliberately narrow: warnings with uncertain data or
# order implications must continue through the fail-closed path below.
_NON_FATAL_IBKR_STATUS_CODES = frozenset({2104, 2106, 2107, 2108, 2158})
_IBKR_CONNECTIVITY_CODES = frozenset({1100, 1101, 1102, 1300})


@dataclass(frozen=True, slots=True)
class ContractIdentity:
    contract_id: int
    symbol: str
    security_type: str
    currency: str


@dataclass(frozen=True, slots=True)
class BrokerOrderBinding:
    broker_order_id: int
    account_ref: str
    environment: Environment
    intent_id: str
    bt_order_ref: int
    requested_qty: int
    side: str
    contract_id: int
    symbol: str
    security_type: str
    currency: str


@dataclass(frozen=True, slots=True)
class PumpResult:
    disposition: str
    event_id: str | None = None
    callback: BrokerCallback | None = None


class BrokerOrderIndex:
    """Durable, trusted binding between IB order ids and local order intent."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS broker_order_binding (
                    broker_order_id INTEGER PRIMARY KEY CHECK(broker_order_id > 0),
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
                    intent_id TEXT NOT NULL UNIQUE,
                    bt_order_ref INTEGER NOT NULL UNIQUE CHECK(bt_order_ref > 0),
                    requested_qty INTEGER NOT NULL CHECK(requested_qty > 0),
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    contract_id INTEGER,
                    symbol TEXT,
                    security_type TEXT,
                    currency TEXT
                );
                CREATE TABLE IF NOT EXISTS callback_inbox (
                    source_key TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
                    delivery_event_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ibkr_callback_spool (
                    callback_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0 CHECK(processed IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(callback_inbox)")}
            if "delivery_event_id" not in columns:
                db.execute("ALTER TABLE callback_inbox ADD COLUMN delivery_event_id TEXT")
            binding_columns = {row[1] for row in db.execute("PRAGMA table_info(broker_order_binding)")}
            for name, declaration in (
                ("contract_id", "INTEGER"), ("symbol", "TEXT"),
                ("security_type", "TEXT"), ("currency", "TEXT"),
            ):
                if name not in binding_columns:
                    db.execute(f"ALTER TABLE broker_order_binding ADD COLUMN {name} {declaration}")

    def bind(self, binding: BrokerOrderBinding) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            binding.broker_order_id, binding.bt_order_ref, binding.requested_qty
        )):
            raise ValueError("order ids and requested quantity must be positive integers")
        if (binding.side not in {"BUY", "SELL"} or not binding.account_ref or not binding.intent_id
                or type(binding.contract_id) is not int or binding.contract_id <= 0
                or not binding.symbol or not binding.security_type or not binding.currency):
            raise ValueError("binding requires account, intent, side, and verified contract identity")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT account_ref,environment,intent_id,bt_order_ref,requested_qty,side,"
                "contract_id,symbol,security_type,currency "
                "FROM broker_order_binding WHERE broker_order_id=?", (binding.broker_order_id,),
            ).fetchone()
            values = (binding.account_ref, binding.environment.value, binding.intent_id,
                      binding.bt_order_ref, binding.requested_qty, binding.side,
                      binding.contract_id, binding.symbol, binding.security_type, binding.currency)
            if row is not None:
                if row != values:
                    db.rollback()
                    raise CallbackPumpFatal("broker order id is already bound to different trusted order data")
                db.commit()
                return
            try:
                db.execute(
                    "INSERT INTO broker_order_binding "
                    "(broker_order_id,account_ref,environment,intent_id,bt_order_ref,requested_qty,side,"
                    "contract_id,symbol,security_type,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (binding.broker_order_id, *values),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise CallbackPumpFatal("intent or Backtrader order ref already has a broker binding") from exc

    def resolve(self, broker_order_id: int) -> BrokerOrderBinding:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT broker_order_id,account_ref,environment,intent_id,bt_order_ref,requested_qty,side,"
                "contract_id,symbol,security_type,currency "
                "FROM broker_order_binding WHERE broker_order_id=?", (broker_order_id,),
            ).fetchone()
        if row is None:
            raise CallbackPumpFatal("callback references an unmapped broker order id")
        if row[7] is None or not row[8] or not row[9] or not row[10]:
            raise CallbackPumpFatal("durable broker order binding has no verified contract identity")
        return BrokerOrderBinding(
            row[0], row[1], Environment(row[2]), row[3], row[4], row[5], row[6],
            row[7], row[8], row[9], row[10]
        )

    def resolve_backtrader_ref(self, bt_order_ref: int) -> BrokerOrderBinding:
        """Resolve only native refs already present in the durable order map."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT broker_order_id FROM broker_order_binding WHERE bt_order_ref=?",
                (bt_order_ref,),
            ).fetchone()
        if row is None:
            raise CallbackPumpFatal("cancel references an unmapped Backtrader order ref")
        return self.resolve(int(row[0]))

    def resolve_intent(self, intent_id: str) -> BrokerOrderBinding:
        """Resolve an app-owned intent to its exact durable broker mapping."""
        if not isinstance(intent_id, str) or not intent_id:
            raise ValueError("intent_id is required")
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT broker_order_id FROM broker_order_binding WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise CallbackPumpFatal("intent has no durable broker order mapping")
        return self.resolve(int(row[0]))

    def persist_inbox(self, source_key: str, payload: dict[str, Any], event: BrokerEvent) -> BrokerEvent:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        event_json = canonical_json(event)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload_sha256,event_json FROM callback_inbox WHERE source_key=?", (source_key,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    db.rollback()
                    raise CallbackPumpFatal("broker callback identity was reused with different content")
                db.commit()
                return BrokerEvent.model_validate_json(row[1])
            db.execute(
                "INSERT INTO callback_inbox(source_key,payload_sha256,event_json) VALUES(?,?,?)",
                (source_key, digest, event_json),
            )
            db.commit()
        return event

    def pending_inbox(self) -> list[tuple[str, BrokerEvent]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT source_key,event_json FROM callback_inbox WHERE delivered=0 ORDER BY rowid"
            ).fetchall()
        return [(row[0], BrokerEvent.model_validate_json(row[1])) for row in rows]

    def pending_delivery_event_ids(self) -> set[str]:
        """Return native callback IDs durably awaiting the Backtrader ACK."""
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT delivery_event_id FROM callback_inbox "
                "WHERE delivered=0 AND delivery_event_id IS NOT NULL"
            ).fetchall()
        values = [row[0] for row in rows]
        if any(not isinstance(value, str) or not value for value in values):
            raise CallbackPumpFatal("durable callback delivery identity is invalid")
        if len(set(values)) != len(values):
            raise CallbackPumpFatal("durable callback delivery ID is bound to multiple inbox rows")
        return set(values)

    def mark_delivered(self, source_key: str) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE callback_inbox SET delivered=1 WHERE source_key=?", (source_key,))
            db.commit()

    def set_delivery_event(self, source_key: str, event_id: str) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "UPDATE callback_inbox SET delivery_event_id=? WHERE source_key=? AND delivered=0",
                (event_id, source_key),
            )
            db.commit()

    def acknowledge_delivery(self, event_id: str) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "UPDATE callback_inbox SET delivered=1 WHERE delivery_event_id=?",
                (event_id,),
            )
            db.commit()

    def is_delivered(self, source_key: str) -> bool:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT delivered FROM callback_inbox WHERE source_key=?", (source_key,)
            ).fetchone()
        return row is not None and bool(row[0])

    def delivered_event(self, source_key: str) -> BrokerEvent | None:
        """Return the committed event for an exact already-delivered callback."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT event_json FROM callback_inbox WHERE source_key=? AND delivered=1",
                (source_key,),
            ).fetchone()
        return BrokerEvent.model_validate_json(row[0]) if row is not None else None

    def pending_callbacks(self) -> list[tuple[str, Any]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT callback_hash,kind,payload_json FROM ibkr_callback_spool "
                "WHERE processed=0 ORDER BY rowid"
            ).fetchall()
        return [
            (row[0], SimpleNamespace(kind=row[1], payload=tuple(sorted(json.loads(row[2]).items()))))
            for row in rows
        ]

    def mark_callback_processed(self, callback_hash: str) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "UPDATE ibkr_callback_spool SET processed=1 WHERE callback_hash=?", (callback_hash,)
            )
            db.commit()


class IbkrCallbackPump:
    """Consume a bounded SDK queue and deliver committed cumulative facts."""

    def __init__(
        self,
        *,
        events: queue.Queue,
        overflowed: Any,
        ledger: SQLiteOrderLedger,
        order_index: BrokerOrderIndex,
        broker: str,
        account_ref: str,
        environment: Environment,
        callback_sink: Callable[[BrokerCallback], None],
        market_timezone: str = "America/New_York",
        now: Callable[[], datetime] | None = None,
        pause_latch: Any | None = None,
        defer_delivery_ack: bool = False,
        next_valid_id_sink: Callable[[int], None] | None = None,
        reconciliation_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.events = events
        self.overflowed = overflowed
        self.ledger = ledger
        self.order_index = order_index
        self.broker = broker
        self.account_ref = account_ref
        self.environment = Environment(environment)
        self.callback_sink = callback_sink
        self.market_tz = ZoneInfo(market_timezone)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.pause_latch = pause_latch
        self.defer_delivery_ack = defer_delivery_ack
        self.next_valid_id_sink = next_valid_id_sink
        self.reconciliation_sink = reconciliation_sink
        self.next_valid_id_seen = threading.Event()
        self._next_valid_id_generation = 0
        self.managed_accounts_seen = threading.Event()
        self._fatal: CallbackPumpFatal | None = None
        # ``callback_inbox`` is the durable source of truth. This set only
        # suppresses repeated handoff within this process while Backtrader has
        # not ACKed a callback yet. A newly constructed pump intentionally
        # starts with an empty set, so an unacknowledged inbox row is replayed
        # after process recovery.
        self._delivery_lock = threading.RLock()
        self._awaiting_delivery_ack: dict[str, str] = {}
        self._background_worker_registered = False

    @property
    def background_worker_registered(self) -> bool:
        """Whether a dedicated worker owns this pump's callback-consumer role."""
        return self._background_worker_registered

    @property
    def next_valid_id_generation(self) -> int:
        """Monotone count of allocator-observed nextValidId callbacks."""
        return self._next_valid_id_generation

    def register_background_worker(self) -> None:
        """Reserve the single background-consumer role before worker start."""
        with self._delivery_lock:
            if self._background_worker_registered:
                raise RuntimeError("callback pump already has a dedicated background worker")
            self._background_worker_registered = True

    def bind_order(self, binding: BrokerOrderBinding) -> None:
        """Persist a broker ID only after matching it to the committed intent."""
        self._check_scope(binding)
        intent = self.ledger.get_intent(binding.intent_id)
        record = self.ledger.get_order(binding.intent_id)
        if (
            intent.broker != self.broker
            or intent.account_ref != binding.account_ref
            or intent.environment is not binding.environment
            or intent.integer_qty != binding.requested_qty
            or intent.side != binding.side
            or intent.instrument_id != binding.symbol
            or binding.security_type != "STK"
            or type(binding.contract_id) is not int or binding.contract_id <= 0
            or not binding.currency
        ):
            self._fail("broker order binding differs from the durable authorized intent")
        if record.state not in {LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN}:
            self._fail("broker order binding requires a durably started or unresolved send")
        self.order_index.bind(binding)

    def process_one(self) -> PumpResult:
        """Replay oldest durable delivery first, then consume one queued event."""
        if self._fatal is not None:
            raise self._fatal
        if self.overflowed.is_set():
            self._fail("IBKR callback queue overflowed; execution is paused")
        pending = self.order_index.pending_inbox()
        for source_key, event in pending:
            with self._delivery_lock:
                already_queued = source_key in self._awaiting_delivery_ack.values()
            if not already_queued:
                return self._apply_and_deliver(source_key, event)
        durable_callbacks = self.order_index.pending_callbacks()
        if durable_callbacks:
            callback_hash, raw = durable_callbacks[0]
            try:
                if raw.kind == "commission":
                    result = self._record_commission(raw)
                    self.order_index.mark_callback_processed(callback_hash)
                    return result
                event = self._normalize(raw)
                if event is None:
                    self.order_index.mark_callback_processed(callback_hash)
                    return PumpResult("IGNORED")
                payload = dict(raw.payload)
                source_key = self._source_key(raw.kind, payload)
                durable = self.order_index.persist_inbox(source_key, payload, event)
                # Once the normalized ledger event is committed, retry safety
                # moves to callback_inbox if this process stops before apply.
                self.order_index.mark_callback_processed(callback_hash)
                if self.order_index.is_delivered(source_key):
                    return PumpResult("DUPLICATE", durable.broker_event_id)
                return self._apply_and_deliver(source_key, durable)
            except CallbackPumpFatal as exc:
                self._fail(str(exc), exc)
            except Exception as exc:
                self._fail(f"durable callback spool processing failed: {exc}", exc)
        try:
            raw = self.events.get_nowait()
        except queue.Empty:
            return PumpResult("EMPTY")
        try:
            if raw.kind == "commission":
                return self._record_commission(raw)
            payload = dict(raw.payload)
            source_key = self._source_key(raw.kind, payload)
            prior = self.order_index.delivered_event(source_key)
            if prior is not None:
                return PumpResult("DUPLICATE", prior.broker_event_id)
            event = self._normalize(raw)
            if event is None:
                return PumpResult("IGNORED")
            durable = self.order_index.persist_inbox(source_key, payload, event)
            if self.order_index.is_delivered(source_key):
                return PumpResult("DUPLICATE", durable.broker_event_id)
            return self._apply_and_deliver(source_key, durable)
        except CallbackPumpFatal as exc:
            self._fail(str(exc), exc)
        except (LedgerConflict, ValueError, InvalidOperation, KeyError, TypeError) as exc:
            self._fail(f"invalid or conflicting IBKR callback: {exc}", exc)
        except Exception as exc:
            self._fail(f"unexpected callback processing failure: {exc}", exc)
        raise AssertionError("unreachable")

    def record_callback(self, raw: Any) -> None:
        """Durably spool a normalized SDK callback before adapter queueing.

        This is useful when the SDK adapter and pump share a process. For a
        separately launched IBKR runtime, pass ``SQLiteCallbackSpool.append``
        as the adapter's required durable_sink and point both at this same DB;
        the pump consumes that primitive spool itself. Informational events are
        ignored only after the queue handoff path remains observable.
        """
        if self._fatal is not None:
            raise self._fatal
        try:
            if raw.kind == "commission":
                self._record_commission(raw)
                return
            event = self._normalize(raw)
            if event is None:
                return
            payload = dict(raw.payload)
            source_key = self._source_key(raw.kind, payload)
            self.order_index.persist_inbox(source_key, payload, event)
        except Exception as exc:
            self._fail(f"could not durably record SDK callback: {exc}", exc)

    def _apply_and_deliver(self, source_key: str, event: BrokerEvent) -> PumpResult:
        try:
            self.ledger.record_broker_event(event)
            current = self.ledger.get_order(event.intent_id)
            binding = self.order_index.resolve(int(event.broker_order_id or self._order_id_from_source(source_key)))
            callback = self._cumulative_callback(binding, current, event)
            if callback is not None:
                if self.defer_delivery_ack:
                    self.order_index.set_delivery_event(source_key, callback.event_id)
                    with self._delivery_lock:
                        previous_source = self._awaiting_delivery_ack.get(callback.event_id)
                        if previous_source is not None:
                            if previous_source != source_key:
                                self._fail("callback delivery event id is bound to multiple inbox records")
                            return PumpResult("WAITING_ACK", callback.event_id, callback)
                        self._awaiting_delivery_ack[callback.event_id] = source_key
                    try:
                        self.callback_sink(callback)
                    except Exception:
                        # The sink failed before accepting the handoff (for
                        # example, a bounded PortBroker queue filled). Keep the
                        # durable inbox replayable in this same process.
                        with self._delivery_lock:
                            if self._awaiting_delivery_ack.get(callback.event_id) == source_key:
                                del self._awaiting_delivery_ack[callback.event_id]
                        raise
                else:
                    self.callback_sink(callback)
            if callback is None or not self.defer_delivery_ack:
                self.order_index.mark_delivered(source_key)
            return PumpResult("APPLIED", event.broker_event_id, callback)
        except Exception as exc:
            self._fail(f"callback apply or delivery failed: {exc}", exc)
        raise AssertionError("unreachable")

    def acknowledge_delivery(self, event_id: str) -> None:
        """Commit the consumer ACK and release this process's in-flight claim."""
        self.order_index.acknowledge_delivery(event_id)
        with self._delivery_lock:
            self._awaiting_delivery_ack.pop(event_id, None)

    def rebind_callback_sink_after_quiescence(
        self,
        callback_sink: Callable[[BrokerCallback], None],
        *,
        expected_pending_event_ids: set[str],
    ) -> None:
        """Switch a dead consumer's sink and release only replayable claims.

        The callback worker must already be paused at its ``process_one``
        boundary. Durable inbox rows remain authoritative; clearing this
        process-local suppression set makes them replay to the replacement
        sink, whose recovered native orders can ACK them idempotently.
        """
        if not callable(callback_sink):
            raise TypeError("callback_sink must be callable")
        if type(expected_pending_event_ids) is not set or any(
            not isinstance(value, str) or not value for value in expected_pending_event_ids
        ):
            raise ValueError("expected pending delivery IDs must be a set of non-empty strings")
        if not self.defer_delivery_ack or not self.background_worker_registered:
            raise CallbackPumpFatal("delivery sink replacement requires deferred background ACK ownership")
        durable_ids = self.order_index.pending_delivery_event_ids()
        if durable_ids != expected_pending_event_ids:
            raise CallbackPumpFatal("durable pending callback identities changed during sink replacement")
        with self._delivery_lock:
            claimed_ids = set(self._awaiting_delivery_ack)
            if not claimed_ids.issubset(durable_ids):
                raise CallbackPumpFatal("in-memory callback claims have no durable replay source")
            self.callback_sink = callback_sink
            # Inbox rows, not these process-local claims, decide what gets
            # replayed. Preserve no claim across a broker/native-registry swap.
            self._awaiting_delivery_ack.clear()

    @staticmethod
    def _order_id_from_source(source_key: str) -> int:
        # The normalized ledger event always carries this id for IB callbacks;
        # this fallback is only used when recovering older inbox rows.
        try:
            return int(source_key.rsplit(":", 1)[1])
        except (ValueError, IndexError) as exc:
            raise CallbackPumpFatal("durable callback inbox is missing its order binding") from exc

    def _normalize(self, raw: Any) -> BrokerEvent | None:
        kind = raw.kind
        payload = dict(raw.payload)
        now = self._utc_now()
        if kind == "next_valid_id":
            order_id = _as_int(payload, "order_id")
            if self.next_valid_id_sink is None:
                raise CallbackPumpFatal("nextValidId arrived without a durable order-id allocator")
            self.next_valid_id_sink(order_id)
            self._next_valid_id_generation += 1
            self.next_valid_id_seen.set()
            return None
        if kind == "managed_accounts":
            accounts_value = payload.get("accounts")
            if not isinstance(accounts_value, str) or not accounts_value.strip():
                raise CallbackPumpFatal("managedAccounts callback is empty or malformed")
            account_rows = [account.strip() for account in accounts_value.split(",")]
            if any(not account for account in account_rows):
                raise CallbackPumpFatal("managedAccounts callback contains an empty account entry")
            accounts = set(account_rows)
            if len(account_rows) != 1 or accounts != {self.account_ref}:
                raise CallbackPumpFatal(
                    "managedAccounts callback does not exactly match the configured single-account PAPER scope"
                )
            self.managed_accounts_seen.set()
            return None
        if kind in {"account_summary", "position", "account_summary_end", "positions_end", "open_orders_end"}:
            self._record_reconciliation_event(kind, payload)
            return None
        if kind == "commission":
            raise CallbackPumpFatal("commission callbacks must use the append-only fee-fact path")
        if kind == "error":
            error_code = payload.get("error_code")
            if type(error_code) is not int:
                raise CallbackPumpFatal("IBKR error callback has an invalid code; execution paused")
            if error_code in _NON_FATAL_IBKR_STATUS_CODES:
                # These narrowly documented farm-connected/inactive notices do
                # not change order or execution facts. They are still recorded
                # in the adapter's durable SDK spool before being acknowledged.
                return None
            if error_code == 201:
                # IBKR's order-rejected callback uses reqId for the order id.
                # Trust that relationship only when it resolves to our durable
                # order binding, and only before any execution/terminal race.
                order_id = _as_int(payload, "request_id")
                binding = self.order_index.resolve(order_id)
                self._check_scope(binding)
                current = self.ledger.get_order(binding.intent_id)
                if current.filled_qty != 0 or current.state not in {
                    LocalOrderState.SEND_STARTED,
                    LocalOrderState.UNKNOWN,
                    LocalOrderState.ACKNOWLEDGED,
                }:
                    raise CallbackPumpFatal(
                        "IBKR order rejection conflicts with fills or current order state; reconciliation required"
                    )
                return BrokerEvent(
                    broker=self.broker,
                    account_ref=binding.account_ref,
                    environment=binding.environment,
                    intent_id=binding.intent_id,
                    broker_event_id="ib-reject:" + _digest(payload),
                    kind=BrokerEventKind.REJECT,
                    occurred_at=now,
                    received_at=now,
                    broker_order_id=str(order_id),
                )
            if error_code in _IBKR_CONNECTIVITY_CODES:
                raise CallbackPumpFatal(
                    f"IBKR connectivity status {error_code} requires reconnect and account/order reconciliation"
                )
            # Unknown errors/warnings and errors without a dedicated durable
            # transition stay fail-closed; do not infer their semantics.
            raise CallbackPumpFatal(
                f"unclassified IBKR error/status code {error_code}; execution paused for review"
            )
        if kind == "open_order":
            if payload.get("snapshot_id") is not None:
                self._record_reconciliation_event(kind, payload)
            binding = self.order_index.resolve(_as_int(payload, "order_id"))
            self._check_scope(binding)
            self._check_contract(binding, payload)
            return None
        if kind == "order_status":
            order_id = _as_int(payload, "order_id")
            binding = self.order_index.resolve(order_id)
            self._check_scope(binding)
            status = str(payload.get("status", ""))
            if status == "Filled":
                # IB's orderStatus is cumulative but has no execId or execution
                # price facts. It cannot create a ledger fill. Accept it only
                # as a duplicate terminal marker after executions already
                # reconcile the full quantity; otherwise preserve the durable
                # callback and fail closed for execution-detail reconciliation.
                reported_filled = _decimal(payload, "filled")
                current = self.ledger.get_order(binding.intent_id)
                if (
                    reported_filled != Decimal(binding.requested_qty)
                    or current.state is not LocalOrderState.FILLED
                    or current.filled_qty != binding.requested_qty
                ):
                    raise CallbackPumpFatal(
                        "IBKR Filled status lacks matching execution details in the order ledger; "
                        "reconciliation is required"
                    )
            mapping = {
                "Submitted": BrokerEventKind.ACK,
                "PreSubmitted": BrokerEventKind.ACK,
                "Filled": BrokerEventKind.ACK,
                "PendingCancel": BrokerEventKind.CANCEL_PENDING,
                "Cancelled": BrokerEventKind.CANCELLED,
                "ApiCancelled": BrokerEventKind.CANCELLED,
            }
            if status not in mapping:
                raise CallbackPumpFatal(f"unsupported IBKR order status {status!r}; manual review required")
            return BrokerEvent(
                broker=self.broker, account_ref=binding.account_ref, environment=binding.environment,
                intent_id=binding.intent_id,
                broker_event_id="ib-status:" + _digest({k: payload[k] for k in sorted(payload)}),
                kind=mapping[status], occurred_at=now, received_at=now,
                broker_order_id=str(order_id),
            )
        if kind == "execution":
            order_id = _as_int(payload, "order_id")
            binding = self.order_index.resolve(order_id)
            self._check_scope(binding)
            self._check_contract(binding, payload)
            callback_account = str(payload.get("account", ""))
            if callback_account != self.account_ref:
                raise CallbackPumpFatal("execution callback account does not match configured account")
            side = {"BOT": "BUY", "SLD": "SELL"}.get(str(payload.get("side", "")))
            if side != binding.side:
                raise CallbackPumpFatal("execution side does not match the durable order binding")
            qty_decimal = _decimal(payload, "shares")
            if qty_decimal != qty_decimal.to_integral_value() or qty_decimal <= 0:
                raise CallbackPumpFatal("execution quantity must be a positive whole share count")
            price = _decimal(payload, "price")
            if price <= 0:
                raise CallbackPumpFatal("execution price must be positive")
            execution_id = str(payload.get("execution_id", ""))
            if not execution_id:
                raise CallbackPumpFatal("execution callback is missing execId")
            occurred = _parse_ib_time(str(payload.get("time", "")), self.market_tz)
            return BrokerEvent(
                broker=self.broker, account_ref=binding.account_ref, environment=binding.environment,
                intent_id=binding.intent_id, broker_event_id="ib-exec:" + _digest(payload),
                kind=BrokerEventKind.FILL, occurred_at=occurred, received_at=now,
                broker_order_id=str(order_id), broker_exec_id=execution_id,
                quantity=int(qty_decimal), price=price,
            )
        raise CallbackPumpFatal(f"unsupported callback kind {kind!r}; execution paused")

    def _record_commission(self, raw: Any) -> PumpResult:
        """Persist one normalized commission report without order association assumptions."""
        try:
            payload = dict(raw.payload)
            callback_account = payload.get("account")
            if callback_account is not None and str(callback_account) != self.account_ref:
                raise CallbackPumpFatal("commission callback account does not match configured account")
            execution_id = payload.get("execution_id", payload.get("exec_id"))
            if not isinstance(execution_id, str) or not execution_id.strip():
                raise CallbackPumpFatal("commission callback is missing execution id")
            currency = payload.get("currency")
            if not isinstance(currency, str) or not currency.strip():
                raise CallbackPumpFatal("commission callback is missing currency")
            amount = _decimal(payload, "commission")
            received = self._utc_now()
            inserted = self.ledger.append_fee_fact(
                broker=self.broker, account_ref=self.account_ref, environment=self.environment,
                broker_exec_id=execution_id, amount=amount, currency=currency.strip().upper(),
                received_at=received, evidence=payload,
            )
            facts = self.ledger.list_fee_facts(
                broker=self.broker, account_ref=self.account_ref,
                environment=self.environment, broker_exec_id=execution_id,
            )
            revised = len(facts) > 1
            if not inserted:
                prior = next((fact for fact in facts
                              if fact["evidence_sha256"] == _digest(payload)), None)
                if prior is None:
                    raise CallbackPumpFatal("exact commission replay has no matching durable fee fact")
                return PumpResult("FEE_DUPLICATE", prior["fee_fact_id"])
            return PumpResult("FEE_PENDING_REVIEW" if revised else "FEE_RECORDED",
                              facts[-1]["fee_fact_id"])
        except CallbackPumpFatal:
            raise
        except Exception as exc:
            self._fail(f"invalid or conflicting commission callback: {exc}", exc)
        raise AssertionError("unreachable")

    def _record_reconciliation_event(self, kind: str, payload: dict[str, Any]) -> None:
        snapshot_id = payload.get("snapshot_id")
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise CallbackPumpFatal("broker reconciliation callback has no valid snapshot identity")
        if self.reconciliation_sink is None:
            raise CallbackPumpFatal("broker reconciliation callback has no trusted snapshot consumer")
        try:
            self.reconciliation_sink(kind, payload)
        except Exception as exc:
            self._fail(f"broker reconciliation callback could not be committed: {exc}", exc)

    def _cumulative_callback(self, binding, current, event) -> BrokerCallback | None:
        state = current.state
        status_by_state = {
            LocalOrderState.ACKNOWLEDGED: CallbackStatus.ACCEPTED,
            LocalOrderState.PARTIALLY_FILLED: CallbackStatus.PARTIALLY_FILLED,
            LocalOrderState.FILLED: CallbackStatus.FILLED,
            LocalOrderState.CANCEL_PENDING: CallbackStatus.ACCEPTED,
            LocalOrderState.CANCELLED: CallbackStatus.CANCELLED,
            LocalOrderState.REJECTED: CallbackStatus.REJECTED,
        }
        status = status_by_state.get(state)
        if status is None:
            raise CallbackPumpFatal(f"ledger state {state.value} cannot be delivered to Backtrader")
        # No fee callback is yet persisted; keep the proxy at zero commissions
        # and stop when the SDK reports a commission adjustment.
        callback_key = {
            "order_ref": binding.bt_order_ref,
            "state": state.value,
            "filled_qty": current.filled_qty,
            "average_fill_price": str(current.average_fill_price) if current.average_fill_price else None,
            "occurred_at": event.occurred_at.isoformat(),
        }
        event_id = "ib-ledger:" + _digest(callback_key)
        return BrokerCallback(
            order_ref=binding.bt_order_ref,
            status=status,
            event_id=event_id,
            occurred_at=event.occurred_at,
            filled_qty=current.filled_qty,
            average_fill_price=(float(current.average_fill_price) if current.average_fill_price else None),
            cumulative_commission=0.0,
        )

    def _check_scope(self, binding: BrokerOrderBinding) -> None:
        if binding.account_ref != self.account_ref or binding.environment is not self.environment:
            raise CallbackPumpFatal("durable broker order binding is outside configured account/environment")

    @staticmethod
    def _check_contract(binding: BrokerOrderBinding, payload: dict[str, Any]) -> None:
        try:
            actual_id = _as_int(payload, "contract_id")
            actual = (
                actual_id, str(payload.get("symbol", "")),
                str(payload.get("security_type", "")), str(payload.get("currency", "")),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise CallbackPumpFatal("IBKR callback is missing verified contract identity") from exc
        expected = (binding.contract_id, binding.symbol, binding.security_type, binding.currency)
        if actual != expected:
            raise CallbackPumpFatal("IBKR callback contract identity differs from the durable order binding")

    def _utc_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CallbackPumpFatal("pump clock must return an aware timestamp")
        return value.astimezone(timezone.utc)

    def _fail(self, message: str, cause: BaseException | None = None):
        error = CallbackPumpFatal(message)
        self._fatal = error
        if self.pause_latch is not None:
            self.pause_latch.set()
        if cause is not None:
            raise error from cause
        raise error

    @staticmethod
    def _source_key(kind: str, payload: dict[str, Any]) -> str:
        if kind == "execution":
            # Distinct callback envelopes for one execId reach the ledger,
            # which deduplicates on execId while detecting changed fill facts.
            # Exact transport replay is deduped here by the full payload hash.
            return f"execution:{_digest(payload)}:{payload.get('order_id', 0)}"
        # Include the order id in the suffix so recovery can resolve inbox rows.
        return f"status:{_digest(payload)}:{payload.get('order_id', 0)}"


class IbkrCallbackPumpWorker:
    """Run the trusted durable callback consumer independently of Cerebro.

    ``PortBroker`` callback sinks are thread-safe queues, so this worker may
    commit broker facts and enqueue native notifications while a strategy is
    blocked or between bars. It does not own the SDK process or activate order
    dispatch. Any pump failure is retained and leaves the supplied pause latch
    set by ``IbkrCallbackPump``.
    """

    def __init__(
        self,
        pump: IbkrCallbackPump,
        *,
        poll_interval: float = 0.05,
        failure_sink: Callable[[BaseException], None] | None = None,
    ) -> None:
        if not isinstance(pump, IbkrCallbackPump):
            raise TypeError("pump must be an IbkrCallbackPump")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not 0.001 <= poll_interval <= 5:
            raise ValueError("poll_interval must be between 1 ms and 5 seconds")
        if failure_sink is not None and not callable(failure_sink):
            raise TypeError("failure_sink must be callable or None")
        self.pump = pump
        self.pump.register_background_worker()
        self.poll_interval = float(poll_interval)
        self.failure_sink = failure_sink
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._run_gate = threading.Event()
        self._run_gate.set()
        self._process_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._failure_sink_error: BaseException | None = None

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def failure_sink_error(self) -> BaseException | None:
        return self._failure_sink_error

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("callback pump worker cannot be started twice")
        self._thread = threading.Thread(
            target=self._run, name="ibkr-callback-pump", daemon=False,
        )
        self._thread.start()

    def wake(self) -> None:
        """Wake the poll loop after the parent observes new durable callbacks."""
        self._wake.set()

    def pause(self, *, timeout: float = 5.0) -> bool:
        """Stop consuming callbacks after the current process_one call drains."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
            raise ValueError("pause timeout must be between 0 and 30 seconds")
        self._run_gate.clear()
        if self._thread is None:
            return True
        acquired = self._process_lock.acquire(timeout=float(timeout))
        if not acquired:
            return False
        self._process_lock.release()
        return True

    def resume(self) -> None:
        """Resume callback consumption after the native broker registry is ready."""
        if self._stop.is_set():
            raise RuntimeError("stopped callback worker cannot resume")
        self._run_gate.set()
        self._wake.set()

    def rebind_delivery_sink(
        self,
        callback_sink: Callable[[BrokerCallback], None],
        *,
        expected_pending_event_ids: set[str],
        timeout: float = 5.0,
    ) -> None:
        """Atomically rebind callback delivery while this live worker is paused."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
            raise ValueError("rebind timeout must be between 0 and 30 seconds")
        if not self.alive or self.failure is not None or self._stop.is_set():
            raise RuntimeError("a healthy live callback worker is required for sink replacement")
        if self._run_gate.is_set():
            raise RuntimeError("callback worker must be paused before replacing its delivery sink")
        if not self._process_lock.acquire(timeout=float(timeout)):
            raise TimeoutError("callback worker did not quiesce for sink replacement")
        try:
            if self._run_gate.is_set() or self._stop.is_set() or self.failure is not None:
                raise RuntimeError("callback worker state changed during sink replacement")
            self.pump.rebind_callback_sink_after_quiescence(
                callback_sink,
                expected_pending_event_ids=expected_pending_event_ids,
            )
        finally:
            self._process_lock.release()

    def stop(self, *, timeout: float = 5.0) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        self._stop.set()
        self._run_gate.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=float(timeout))
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._run_gate.wait(self.poll_interval):
                continue
            if self._stop.is_set():
                break
            self._process_lock.acquire()
            if self._stop.is_set() or not self._run_gate.is_set():
                self._process_lock.release()
                continue
            try:
                result = self.pump.process_one()
            except BaseException as exc:
                self._process_lock.release()
                self._failure = exc
                if self.failure_sink is not None:
                    try:
                        self.failure_sink(exc)
                    except BaseException as sink_exc:
                        self._failure_sink_error = sink_exc
                return
            self._process_lock.release()
            if result.disposition in {"EMPTY", "WAITING_ACK"}:
                self._wake.wait(self.poll_interval)
                self._wake.clear()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_int(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value <= 0:
        raise CallbackPumpFatal(f"{key} must be a positive integer")
    return value


def _decimal(payload: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(payload[key]))
    except (InvalidOperation, KeyError, TypeError) as exc:
        raise CallbackPumpFatal(f"{key} must be a decimal number") from exc
    if not value.is_finite():
        raise CallbackPumpFatal(f"{key} must be finite")
    return value


def _parse_ib_time(value: str, market_tz: ZoneInfo) -> datetime:
    for pattern in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), pattern).replace(tzinfo=market_tz).astimezone(timezone.utc)
        except ValueError:
            continue
    raise CallbackPumpFatal("execution time has an unsupported or missing IBKR timestamp format")
