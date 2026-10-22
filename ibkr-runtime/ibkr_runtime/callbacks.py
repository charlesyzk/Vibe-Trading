"""Bounded, normalized callbacks for the official IBKR TWS API.

The SDK callback thread only publishes immutable values. Consumers must apply
those values from their own event loop and must pause on queue overflow because
silently dropping an order or execution callback would corrupt state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal
from queue import Full, Queue
from threading import Event, Lock
from typing import Any, Callable

from ibapi.wrapper import EWrapper


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    kind: str
    payload: tuple[tuple[str, str | int | bool | None], ...]

    @classmethod
    def create(cls, kind: str, **payload: str | int | bool | None) -> "CallbackEvent":
        return cls(kind, tuple(sorted(payload.items())))


class SQLiteCallbackSpool:
    """Durable primitive callback spool shared with the trusted host service."""

    def __init__(self, path: str | Path, *, max_event_bytes: int = 64 * 1024) -> None:
        if type(max_event_bytes) is not int or max_event_bytes < 1:
            raise ValueError("max_event_bytes must be a positive integer")
        self.path = str(path)
        self.max_event_bytes = max_event_bytes
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS ibkr_callback_spool (
                    callback_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0 CHECK(processed IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.commit()

    def append(self, event: CallbackEvent) -> None:
        """Commit a callback before the SDK adapter attempts queue handoff."""
        if not isinstance(event, CallbackEvent):
            raise TypeError("callback spool only accepts normalized CallbackEvent values")
        payload_json = json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False)
        body = json.dumps({"kind": event.kind, "payload": json.loads(payload_json)},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(body.encode("utf-8")) > self.max_event_bytes:
            raise ValueError("callback event exceeds durable spool byte limit")
        callback_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO ibkr_callback_spool(callback_hash,kind,payload_json) VALUES(?,?,?)",
                (callback_hash, event.kind, payload_json),
            )
            db.commit()

    def pending(self, *, limit: int = 256) -> list[tuple[int, CallbackEvent]]:
        """Read an ordered bounded batch without acknowledging it."""
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("limit must be an integer from 1 through 4096")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT rowid,kind,payload_json FROM ibkr_callback_spool "
                "WHERE processed=0 ORDER BY rowid LIMIT ?", (limit,),
            ).fetchall()
        events: list[tuple[int, CallbackEvent]] = []
        for rowid, kind, payload_json in rows:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError("callback spool payload is corrupt")
            events.append((int(rowid), CallbackEvent.create(kind, **payload)))
        return events

    def mark_processed(self, sequence: int) -> None:
        """Acknowledge one event only after the trusted ledger consumer commits it."""
        if type(sequence) is not int or sequence < 1:
            raise ValueError("sequence must be a positive integer")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE ibkr_callback_spool SET processed=1 WHERE rowid=? AND processed=0",
                (sequence,),
            )
            if cursor.rowcount != 1:
                db.rollback()
                raise KeyError("callback spool event is missing or already processed")
            db.commit()


class IbapiCallbackAdapter(EWrapper):
    """Normalize official SDK callbacks without mutating broker or ledger state."""

    def __init__(
        self,
        events: Queue[CallbackEvent],
        overflowed: Event | None = None,
        *,
        durable_sink: Callable[[CallbackEvent], None],
    ) -> None:
        super().__init__()
        self.events = events
        self.overflowed = overflowed or Event()
        self.next_valid_id_seen = Event()
        self.managed_accounts_seen = Event()
        self._snapshot_lock = Lock()
        self._snapshot_id: int | None = None
        self._snapshot_ends: set[str] = set()
        self.cancel_account_summary: Callable[[int], None] | None = None
        self.cancel_positions: Callable[[], None] | None = None
        if not callable(durable_sink):
            raise TypeError("a durable_sink callback is required")
        self.durable_sink = durable_sink

    def _publish(self, kind: str, **payload: str | int | bool | None) -> None:
        if self.overflowed.is_set():
            return
        event = CallbackEvent.create(kind, **payload)
        # If supplied, durably record the normalized primitive before the
        # in-memory queue can acknowledge it. The trusted consumer can replay
        # the spool after a process restart even if put_nowait never happens.
        try:
            self.durable_sink(event)
        except Exception:
            self.overflowed.set()
            return
        try:
            self.events.put_nowait(event)
        except Full:
            self.overflowed.set()

    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id_seen.set()
        self._publish("next_valid_id", order_id=int(orderId))

    def managedAccounts(self, accountsList: str) -> None:
        self.managed_accounts_seen.set()
        self._publish("managed_accounts", accounts=str(accountsList))

    @property
    def api_ready(self) -> bool:
        """The API handshake callbacks arrived; account/order reconciliation is separate."""
        return self.next_valid_id_seen.is_set() and self.managed_accounts_seen.is_set()

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Decimal,
        remaining: Decimal,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        self._publish(
            "order_status",
            order_id=int(orderId),
            status=str(status),
            filled=str(filled),
            remaining=str(remaining),
            average_fill_price=str(avgFillPrice),
            permanent_id=int(permId),
            parent_id=int(parentId),
            last_fill_price=str(lastFillPrice),
            client_id=int(clientId),
            why_held=str(whyHeld),
            market_cap_price=str(mktCapPrice),
        )

    def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
        self._publish(
            "open_order",
            snapshot_id=self._active_snapshot_id(),
            order_id=int(orderId),
            account=str(getattr(order, "account", "")),
            permanent_id=int(getattr(order, "permId", 0)),
            symbol=str(getattr(contract, "symbol", "")),
            security_type=str(getattr(contract, "secType", "")),
            exchange=str(getattr(contract, "exchange", "")),
            primary_exchange=str(getattr(contract, "primaryExchange", "")),
            contract_id=int(getattr(contract, "conId", 0)),
            currency=str(getattr(contract, "currency", "")),
            action=str(getattr(order, "action", "")),
            quantity=str(getattr(order, "totalQuantity", "")),
            order_type=str(getattr(order, "orderType", "")),
            time_in_force=str(getattr(order, "tif", "")),
            outside_rth=bool(getattr(order, "outsideRth", False)),
            status=str(getattr(orderState, "status", "")),
            warning=str(getattr(orderState, "warningText", "")),
        )

    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
        self._publish(
            "execution",
            request_id=int(reqId),
            execution_id=str(getattr(execution, "execId", "")),
            order_id=int(getattr(execution, "orderId", 0)),
            account=str(getattr(execution, "acctNumber", "")),
            side=str(getattr(execution, "side", "")),
            shares=str(getattr(execution, "shares", "")),
            price=str(getattr(execution, "price", "")),
            time=str(getattr(execution, "time", "")),
            symbol=str(getattr(contract, "symbol", "")),
            contract_id=int(getattr(contract, "conId", 0)),
            security_type=str(getattr(contract, "secType", "")),
            currency=str(getattr(contract, "currency", "")),
        )

    def commissionAndFeesReport(self, report: Any) -> None:
        self._publish(
            "commission",
            execution_id=str(getattr(report, "execId", "")),
            commission=str(getattr(report, "commission", "")),
            currency=str(getattr(report, "currency", "")),
            realized_pnl=str(getattr(report, "realizedPNL", "")),
        )

    def position(self, account: str, contract: Any, position: Decimal, avgCost: float) -> None:
        self._publish(
            "position",
            snapshot_id=self._active_snapshot_id(),
            account=str(account),
            symbol=str(getattr(contract, "symbol", "")),
            contract_id=int(getattr(contract, "conId", 0)),
            currency=str(getattr(contract, "currency", "")),
            quantity=str(position),
            average_cost=str(avgCost),
        )

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:
        snapshot_id = self._active_snapshot_id()
        if snapshot_id is not None and int(reqId) != snapshot_id:
            self.overflowed.set()
            return
        self._publish(
            "account_summary",
            request_id=int(reqId),
            snapshot_id=snapshot_id,
            account=str(account),
            tag=str(tag),
            value=str(value),
            currency=str(currency),
        )

    def begin_reconciliation_snapshot(self, snapshot_id: int) -> None:
        """Tag the one in-flight all-account snapshot request set."""
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ValueError("snapshot_id must be a positive integer")
        with self._snapshot_lock:
            if self._snapshot_id is not None:
                raise RuntimeError("a broker reconciliation snapshot is already in progress")
            self._snapshot_id = snapshot_id
            self._snapshot_ends.clear()

    def accountSummaryEnd(self, reqId: int) -> None:
        snapshot_id = self._active_snapshot_id()
        if snapshot_id is not None and int(reqId) != snapshot_id:
            self.overflowed.set()
            return
        if snapshot_id is not None and self.cancel_account_summary is not None:
            try:
                self.cancel_account_summary(int(reqId))
            except Exception:
                self.overflowed.set()
                return
        self._snapshot_end("account_summary_end", request_id=int(reqId))

    def positionEnd(self) -> None:
        if self._active_snapshot_id() is not None and self.cancel_positions is not None:
            try:
                self.cancel_positions()
            except Exception:
                self.overflowed.set()
                return
        self._snapshot_end("positions_end")

    def openOrderEnd(self) -> None:
        self._snapshot_end("open_orders_end")

    def _active_snapshot_id(self) -> int | None:
        with self._snapshot_lock:
            return self._snapshot_id

    def _snapshot_end(self, kind: str, **payload: int) -> None:
        with self._snapshot_lock:
            snapshot_id = self._snapshot_id
            if (
                kind == "account_summary_end"
                and snapshot_id is not None
                and payload.get("request_id") != snapshot_id
            ):
                self.overflowed.set()
                return
            if snapshot_id is not None:
                self._snapshot_ends.add(kind)
                complete = self._snapshot_ends == {
                    "account_summary_end", "positions_end", "open_orders_end",
                }
                if complete:
                    self._snapshot_id = None
                    self._snapshot_ends.clear()
            else:
                complete = False
        self._publish(kind, snapshot_id=snapshot_id, **payload)

    def error(
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        self._publish(
            "error",
            request_id=int(reqId),
            error_time=int(errorTime),
            error_code=int(errorCode),
            message=str(errorString),
            advanced_reject=str(advancedOrderRejectJson),
        )
