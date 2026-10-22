"""Durable receipt of IBKR account/order/position snapshot callbacks.

This store proves that a callback batch was captured through all three SDK
end markers. It deliberately does not declare the snapshot reconciled against
the local order ledger and cannot enable the order-id allocator.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.execution.contracts import Environment
from src.execution.ibkr_callback_pump import BrokerOrderIndex
from src.execution.ibkr_order_ids import IbkrOrderIdAllocator, OrderIdNotReady
from src.execution.order_ledger import LocalOrderState, SQLiteOrderLedger


class ReconciliationSnapshotError(RuntimeError):
    """A broker snapshot is malformed, conflicting, or incomplete."""


class SQLiteIbkrSnapshotStore:
    REQUIRED_ACCOUNT_TAGS = {"NetLiquidation", "TotalCashValue", "AvailableFunds"}
    END_KINDS = {
        "account_summary_end": "account_summary_complete",
        "positions_end": "positions_complete",
        "open_orders_end": "open_orders_complete",
    }

    def __init__(self, database_path: str | Path) -> None:
        self.path = str(Path(database_path).expanduser().resolve())
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ibkr_reconciliation_run (
                    snapshot_id INTEGER PRIMARY KEY CHECK(snapshot_id > 0),
                    account_ref TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK(environment='paper'),
                    account_summary_complete INTEGER NOT NULL DEFAULT 0 CHECK(account_summary_complete IN (0,1)),
                    positions_complete INTEGER NOT NULL DEFAULT 0 CHECK(positions_complete IN (0,1)),
                    open_orders_complete INTEGER NOT NULL DEFAULT 0 CHECK(open_orders_complete IN (0,1)),
                    reconciled INTEGER NOT NULL DEFAULT 0 CHECK(reconciled IN (0,1)),
                    reconciliation_result_json TEXT,
                    requested_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at_utc TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ibkr_reconciliation_fact (
                    snapshot_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id,kind,fact_key),
                    FOREIGN KEY(snapshot_id) REFERENCES ibkr_reconciliation_run(snapshot_id)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(ibkr_reconciliation_run)")}
            if "reconciled" not in columns:
                db.execute(
                    "ALTER TABLE ibkr_reconciliation_run ADD COLUMN reconciled "
                    "INTEGER NOT NULL DEFAULT 0 CHECK(reconciled IN (0,1))"
                )
            if "reconciliation_result_json" not in columns:
                db.execute(
                    "ALTER TABLE ibkr_reconciliation_run ADD COLUMN reconciliation_result_json TEXT"
                )
            if "requested_at_utc" not in columns:
                db.execute(
                    "ALTER TABLE ibkr_reconciliation_run ADD COLUMN requested_at_utc "
                    "TEXT NOT NULL DEFAULT ''"
                )
                db.execute(
                    "UPDATE ibkr_reconciliation_run SET requested_at_utc=created_at "
                    "WHERE requested_at_utc=''"
                )
            if "completed_at_utc" not in columns:
                db.execute(
                    "ALTER TABLE ibkr_reconciliation_run ADD COLUMN completed_at_utc TEXT"
                )
            db.commit()

    def begin(self, snapshot_id: int, *, account_ref: str, environment: str = "paper") -> None:
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ValueError("snapshot_id must be a positive integer")
        if not isinstance(account_ref, str) or not account_ref.strip():
            raise ValueError("account_ref is required")
        if environment != "paper":
            raise ValueError("IBKR account snapshots are PAPER-only in V1")
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO ibkr_reconciliation_run"
                    "(snapshot_id,account_ref,environment,requested_at_utc) VALUES(?,?,?,?)",
                    (snapshot_id, account_ref, environment,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ReconciliationSnapshotError("snapshot id has already been used") from exc

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        if kind not in {"account_summary", "position", "open_order", *self.END_KINDS}:
            raise ReconciliationSnapshotError("unsupported broker reconciliation callback")
        if not isinstance(payload, dict):
            raise ReconciliationSnapshotError("reconciliation callback payload must be an object")
        snapshot_id = payload.get("snapshot_id")
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ReconciliationSnapshotError("callback has no valid snapshot id")

        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT account_ref,account_summary_complete,positions_complete,open_orders_complete,reconciled "
                "FROM ibkr_reconciliation_run WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if run is None:
                db.rollback()
                raise ReconciliationSnapshotError("callback references an unknown snapshot")
            if run[4]:
                db.rollback()
                raise ReconciliationSnapshotError("reconciled snapshot is immutable")
            account_ref = run[0]

            if kind in self.END_KINDS:
                self._record_end(db, kind, payload, snapshot_id)
                db.commit()
                return

            if kind == "account_summary":
                if run[1]:
                    db.rollback()
                    raise ReconciliationSnapshotError("account summary row arrived after its end marker")
                fact_key, normalized = self._account_summary(snapshot_id, account_ref, payload)
            elif kind == "position":
                if run[2]:
                    db.rollback()
                    raise ReconciliationSnapshotError("position row arrived after its end marker")
                fact_key, normalized = self._position(snapshot_id, account_ref, payload)
            else:
                if run[3]:
                    db.rollback()
                    raise ReconciliationSnapshotError("open order arrived after its end marker")
                fact_key, normalized = self._open_order(snapshot_id, account_ref, payload)

            if normalized is None:
                db.commit()  # Other managed accounts are outside this runtime scope.
                return
            encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            prior = db.execute(
                "SELECT payload_sha256 FROM ibkr_reconciliation_fact "
                "WHERE snapshot_id=? AND kind=? AND fact_key=?",
                (snapshot_id, kind, fact_key),
            ).fetchone()
            if prior is not None:
                if prior[0] != digest:
                    db.rollback()
                    raise ReconciliationSnapshotError("snapshot fact identity was reused with different content")
                db.commit()
                return
            db.execute(
                "INSERT INTO ibkr_reconciliation_fact"
                "(snapshot_id,kind,fact_key,payload_sha256,payload_json) VALUES(?,?,?,?,?)",
                (snapshot_id, kind, fact_key, digest, encoded),
            )
            db.commit()

    def complete_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """Return a fully captured snapshot, explicitly still unreconciled."""
        with closing(sqlite3.connect(self.path)) as db:
            run = db.execute(
                "SELECT account_ref,environment,account_summary_complete,positions_complete,open_orders_complete,"
                "reconciled,reconciliation_result_json,requested_at_utc,completed_at_utc "
                "FROM ibkr_reconciliation_run WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if run is None:
                raise ReconciliationSnapshotError("snapshot does not exist")
            if not all(run[2:5]):
                raise ReconciliationSnapshotError("account, position, and open-order end markers are required")
            rows = db.execute(
                "SELECT kind,fact_key,payload_sha256,payload_json FROM ibkr_reconciliation_fact WHERE snapshot_id=? "
                "ORDER BY kind,fact_key",
                (snapshot_id,),
            ).fetchall()
        facts = {"account_summary": [], "position": [], "open_order": []}
        fact_digest_rows: list[tuple[str, str, str]] = []
        for kind, fact_key, payload_sha256, payload_json in rows:
            if kind not in facts:
                raise ReconciliationSnapshotError("snapshot contains an unsupported fact kind")
            try:
                payload = json.loads(payload_json)
                canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ReconciliationSnapshotError("snapshot fact JSON is malformed") from exc
            computed_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
            if canonical_payload != payload_json or computed_hash != payload_sha256:
                raise ReconciliationSnapshotError("snapshot fact payload hash or canonical JSON is invalid")
            if not isinstance(payload, dict):
                raise ReconciliationSnapshotError("snapshot fact payload must be an object")
            facts[kind].append(payload)
            fact_digest_rows.append((kind, fact_key, computed_hash))
        facts_sha256 = hashlib.sha256(
            json.dumps(fact_digest_rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        reconciliation_result = json.loads(run[6]) if run[6] else None
        if run[5]:
            if (
                not isinstance(reconciliation_result, dict)
                or reconciliation_result.get("snapshot_id") != snapshot_id
                or reconciliation_result.get("account_ref") != run[0]
                or reconciliation_result.get("environment") != run[1]
                or reconciliation_result.get("facts_sha256") != facts_sha256
            ):
                raise ReconciliationSnapshotError("reconciliation proof does not match the captured snapshot facts")
        summary_tags = {
            row["tag"] for row in facts["account_summary"] if row["currency"] == "USD"
        }
        if not self.REQUIRED_ACCOUNT_TAGS.issubset(summary_tags):
            raise ReconciliationSnapshotError("required USD account summary tags are missing")
        return {
            "snapshot_id": snapshot_id,
            "account_ref": run[0],
            "environment": run[1],
            "requested_at_utc": run[7],
            "completed_at_utc": run[8],
            "account_summary": facts["account_summary"],
            "positions": facts["position"],
            "open_orders": facts["open_order"],
            "facts_sha256": facts_sha256,
            "capture_complete": True,
            "reconciled": bool(run[5]),
            "reconciliation_result": reconciliation_result,
        }

    def capture_complete(self, snapshot_id: int) -> bool:
        """Check only the durable end-marker barrier, without parsing facts."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT account_summary_complete,positions_complete,open_orders_complete "
                "FROM ibkr_reconciliation_run WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ReconciliationSnapshotError("snapshot does not exist")
        return all(row)

    def mark_reconciled(self, snapshot_id: int, result: dict[str, Any]) -> None:
        if not isinstance(result, dict) or not result:
            raise ValueError("reconciliation result must be a non-empty object")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with closing(sqlite3.connect(self.path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT account_summary_complete,positions_complete,open_orders_complete,reconciled,"
                "reconciliation_result_json FROM ibkr_reconciliation_run WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None or not all(row[:3]):
                db.rollback()
                raise ReconciliationSnapshotError("complete snapshot markers are required before reconciliation")
            if row[3]:
                if row[4] != encoded:
                    db.rollback()
                    raise ReconciliationSnapshotError("reconciliation result differs from prior proof")
                db.commit()
                return
            db.execute(
                "UPDATE ibkr_reconciliation_run SET reconciled=1,reconciliation_result_json=? "
                "WHERE snapshot_id=?",
                (encoded, snapshot_id),
            )
            db.commit()

    def _record_end(self, db: sqlite3.Connection, kind: str, payload: dict[str, Any], snapshot_id: int) -> None:
        if kind == "account_summary_end" and payload.get("request_id") != snapshot_id:
            db.rollback()
            raise ReconciliationSnapshotError("account summary end marker id does not match snapshot")
        column = self.END_KINDS[kind]
        cursor = db.execute(
            f"UPDATE ibkr_reconciliation_run SET {column}=1 WHERE snapshot_id=? AND {column}=0",
            (snapshot_id,),
        )
        # Replayed end callbacks are idempotent when that marker is already set.
        if cursor.rowcount == 0:
            row = db.execute(
                f"SELECT {column} FROM ibkr_reconciliation_run WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None or row[0] != 1:
                db.rollback()
                raise ReconciliationSnapshotError("snapshot end marker could not be recorded")
        flags = db.execute(
            "SELECT account_summary_complete,positions_complete,open_orders_complete "
            "FROM ibkr_reconciliation_run WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if flags is not None and all(flags):
            db.execute(
                "UPDATE ibkr_reconciliation_run SET completed_at_utc=COALESCE(completed_at_utc,?) "
                "WHERE snapshot_id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), snapshot_id),
            )

    def _account_summary(self, snapshot_id: int, account_ref: str, payload: dict[str, Any]):
        if payload.get("request_id") != snapshot_id:
            raise ReconciliationSnapshotError("account summary row request id does not match snapshot")
        account = self._required_text(payload.get("account"), "account summary account")
        if account != account_ref:
            return "", None
        tag = payload.get("tag")
        currency = payload.get("currency")
        if tag not in self.REQUIRED_ACCOUNT_TAGS or currency != "USD":
            raise ReconciliationSnapshotError("unsupported account summary tag or currency")
        value = self._decimal(payload.get("value"), "account summary value")
        if tag == "NetLiquidation" and value <= 0:
            raise ReconciliationSnapshotError("net liquidation must be positive")
        normalized = {
            "snapshot_id": snapshot_id, "account": account_ref,
            "tag": tag, "currency": currency, "value": str(value),
        }
        return f"{account_ref}:{tag}:{currency}", normalized

    def _position(self, snapshot_id: int, account_ref: str, payload: dict[str, Any]):
        account = self._required_text(payload.get("account"), "position account")
        if account != account_ref:
            return "", None
        contract_id = self._positive_int(payload.get("contract_id"), "position contract id")
        symbol = self._required_text(payload.get("symbol"), "position symbol")
        if payload.get("currency") != "USD":
            raise ReconciliationSnapshotError("V1 position snapshot only accepts USD instruments")
        quantity = self._decimal(payload.get("quantity"), "position quantity")
        average_cost = self._decimal(payload.get("average_cost"), "position average cost")
        if quantity != quantity.to_integral_value() or quantity < 0:
            raise ReconciliationSnapshotError("position quantity must be a non-negative whole share count")
        if average_cost < 0:
            raise ReconciliationSnapshotError("position average cost cannot be negative")
        normalized = {
            "snapshot_id": snapshot_id, "account": account_ref, "contract_id": contract_id,
            "symbol": symbol, "currency": "USD", "quantity": str(quantity),
            "average_cost": str(average_cost),
        }
        return f"{account_ref}:{contract_id}", normalized

    def _open_order(self, snapshot_id: int, account_ref: str, payload: dict[str, Any]):
        account = self._required_text(payload.get("account"), "open order account")
        if account != account_ref:
            return "", None
        order_id = self._positive_int(payload.get("order_id"), "open order id")
        contract_id = self._positive_int(payload.get("contract_id"), "open order contract id")
        quantity = self._decimal(payload.get("quantity"), "open order quantity")
        if quantity != quantity.to_integral_value() or quantity <= 0:
            raise ReconciliationSnapshotError("open order quantity must be a positive whole share count")
        normalized = {
            "snapshot_id": snapshot_id, "account": account_ref, "order_id": order_id,
            "permanent_id": self._nonnegative_int(payload.get("permanent_id"), "permanent id"),
            "contract_id": contract_id,
            "symbol": self._required_text(payload.get("symbol"), "open order symbol"),
            "security_type": self._required_text(payload.get("security_type"), "open order security type"),
            "exchange": self._required_text(payload.get("exchange"), "open order exchange"),
            "primary_exchange": self._required_text(payload.get("primary_exchange"), "open order primary exchange"),
            "currency": self._required_text(payload.get("currency"), "open order currency"),
            "action": self._required_text(payload.get("action"), "open order action"),
            "quantity": str(quantity),
            "order_type": self._required_text(payload.get("order_type"), "open order type"),
            "time_in_force": self._required_text(payload.get("time_in_force"), "open order TIF"),
            "outside_rth": payload.get("outside_rth"),
            "status": self._required_text(payload.get("status"), "open order status"),
            "warning": str(payload.get("warning", "")),
        }
        if normalized["action"] not in {"BUY", "SELL"}:
            raise ReconciliationSnapshotError("open order action is unsupported")
        if type(normalized["outside_rth"]) is not bool:
            raise ReconciliationSnapshotError("open order outsideRth flag is malformed")
        return f"{account_ref}:{order_id}", normalized

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ReconciliationSnapshotError(f"{label} must be decimal") from exc
        if not result.is_finite():
            raise ReconciliationSnapshotError(f"{label} must be finite")
        return result

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if type(value) is not int or value <= 0:
            raise ReconciliationSnapshotError(f"{label} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, label: str) -> int:
        if type(value) is not int or value < 0:
            raise ReconciliationSnapshotError(f"{label} must be a non-negative integer")
        return value

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ReconciliationSnapshotError(f"{label} is required")
        return value.strip()


class IbkrReconciliationCoordinator:
    """Correlate one host-created durable snapshot with the SDK child request."""

    def __init__(self, *, store: SQLiteIbkrSnapshotStore, process: Any, account_ref: str) -> None:
        if not isinstance(store, SQLiteIbkrSnapshotStore):
            raise TypeError("store must be SQLiteIbkrSnapshotStore")
        if not callable(getattr(process, "request_reconciliation", None)):
            raise TypeError("process must expose request_reconciliation(snapshot_id)")
        if not isinstance(account_ref, str) or not account_ref.strip():
            raise ValueError("account_ref is required")
        self.store = store
        self.process = process
        self.account_ref = account_ref

    def request_snapshot(self, snapshot_id: int) -> None:
        """Persist intent before issuing IPC so early callbacks always correlate."""
        self.store.begin(snapshot_id, account_ref=self.account_ref)
        try:
            echoed = self.process.request_reconciliation(snapshot_id)
        except Exception:
            # The IPC outcome may be uncertain; preserve the incomplete run so
            # recovery cannot mistake an unobserved request for a clear account.
            raise
        if echoed != snapshot_id:
            raise ReconciliationSnapshotError("runtime acknowledged a different snapshot id")

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.store.record(kind, payload)

    def completed_capture(self, snapshot_id: int) -> dict[str, Any]:
        """Return collected facts; caller must still reconcile them to ledger state."""
        return self.store.complete_snapshot(snapshot_id)


class IbkrRecoveryReconciler:
    """Compare one complete broker snapshot with the durable PAPER ledger.

    The V1 policy is intentionally strict: every broker open order and every
    position must be attributable to this ledger, and every locally active
    order must still appear in the broker open-order snapshot. Any mismatch
    requires a separate executions/history repair path and blocks recovery.
    """

    def __init__(
        self,
        *,
        store: SQLiteIbkrSnapshotStore,
        ledger: SQLiteOrderLedger,
        order_index: BrokerOrderIndex,
        allocator: IbkrOrderIdAllocator,
        account_ref: str,
    ) -> None:
        if not isinstance(store, SQLiteIbkrSnapshotStore):
            raise TypeError("store must be SQLiteIbkrSnapshotStore")
        if not isinstance(ledger, SQLiteOrderLedger) or not isinstance(order_index, BrokerOrderIndex):
            raise TypeError("ledger and order_index must be the shared trusted SQLite components")
        if not isinstance(allocator, IbkrOrderIdAllocator):
            raise TypeError("allocator must be IbkrOrderIdAllocator")
        if not account_ref:
            raise ValueError("account_ref is required")
        expected_path = Path(ledger.path).expanduser().resolve()
        if any(Path(path).expanduser().resolve() != expected_path for path in (
            order_index.path, allocator.path, store.path,
        )):
            raise ValueError("ledger, order index, allocator and snapshots must share one SQLite database")
        self.store = store
        self.ledger = ledger
        self.order_index = order_index
        self.allocator = allocator
        self.account_ref = account_ref

    def reconcile(self, snapshot_id: int) -> dict[str, Any]:
        snapshot = self.store.complete_snapshot(snapshot_id)
        if snapshot["account_ref"] != self.account_ref or snapshot["environment"] != "paper":
            raise ReconciliationSnapshotError("snapshot scope differs from configured PAPER account")
        if snapshot["reconciled"]:
            if self.allocator.allocation_ready:
                return snapshot
            raise ReconciliationSnapshotError(
                "a new broker snapshot is required for this order-id allocator epoch"
            )

        order_result = self._reconcile_open_orders(snapshot)
        position_result = self._reconcile_positions(snapshot)
        candidate = self.allocator.candidate_for_reconciliation()
        known_ids = self._known_order_ids()
        broker_ids = {row["order_id"] for row in snapshot["open_orders"]}
        maximum_seen = max(known_ids | broker_ids, default=0)
        if candidate <= maximum_seen:
            raise ReconciliationSnapshotError(
                "nextValidId candidate is not above the durable and broker order-id high-water mark"
            )

        result = {
            "snapshot_id": snapshot_id,
            "account_ref": self.account_ref,
            "environment": "paper",
            "facts_sha256": snapshot["facts_sha256"],
            "candidate_order_id": candidate,
            "open_order_count": order_result["count"],
            "position_count": position_result["count"],
            "checks": ["scope", "open_orders", "positions", "order_id_high_water_mark"],
        }
        # Persist the proof before opening the allocator gate. If the process
        # stops between these operations, the fresh candidate remains closed.
        self.store.mark_reconciled(snapshot_id, result)
        self.allocator.confirm_reconciled(expected_order_id=candidate)
        return {**snapshot, "reconciled": True, "reconciliation_result": result}

    def verify_reconciled_snapshot_is_current(self, snapshot: dict[str, Any]) -> None:
        """Recheck durable orders and fills after callback delivery is quiesced."""
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("reconciled") is not True
            or snapshot.get("account_ref") != self.account_ref
            or snapshot.get("environment") != "paper"
        ):
            raise ReconciliationSnapshotError("current-snapshot verification requires a reconciled PAPER snapshot")
        self._reconcile_open_orders(snapshot)
        self._reconcile_positions(snapshot)

    def _reconcile_open_orders(self, snapshot: dict[str, Any]) -> dict[str, int]:
        broker_orders: dict[int, dict[str, Any]] = {}
        for order in snapshot["open_orders"]:
            order_id = order["order_id"]
            if order_id in broker_orders:
                raise ReconciliationSnapshotError("broker open-order snapshot repeats an order id")
            try:
                binding = self.order_index.resolve(order_id)
                local = self.ledger.get_order(binding.intent_id)
                intent = self.ledger.get_intent(binding.intent_id)
            except Exception as exc:
                raise ReconciliationSnapshotError("broker open order is not mapped to a durable local intent") from exc
            if (
                binding.account_ref != self.account_ref
                or binding.environment is not Environment.PAPER
                or order["account"] != self.account_ref
                or order["contract_id"] != binding.contract_id
                or order["symbol"] != binding.symbol
                or order["security_type"] != binding.security_type
                or order["currency"] != binding.currency
                or order["action"] != binding.side
                or Decimal(order["quantity"]) != Decimal(binding.requested_qty)
                or intent.account_ref != self.account_ref
                or intent.broker != "IBKR"
                or intent.environment is not Environment.PAPER
                or intent.integer_qty != binding.requested_qty
                or intent.side != binding.side
                or intent.order_type != "market"
                or intent.time_in_force != "DAY"
                or order["security_type"] != "STK"
                or order["currency"] != "USD"
                or order["exchange"] != "SMART"
                or order["time_in_force"] != "DAY"
                or order["order_type"] != "MKT"
                or order["outside_rth"] is not False
                or local.state not in {
                    LocalOrderState.SEND_STARTED, LocalOrderState.UNKNOWN,
                    LocalOrderState.ACKNOWLEDGED, LocalOrderState.PARTIALLY_FILLED,
                    LocalOrderState.CANCEL_PENDING,
                }
            ):
                raise ReconciliationSnapshotError("broker open order differs from its durable local binding")
            broker_orders[order_id] = order

        local_active = self.ledger.recovery_snapshot_for_scope(
            broker="IBKR", account_ref=self.account_ref, environment=Environment.PAPER,
        ).active_orders
        expected_ids: set[int] = set()
        for record in local_active:
            if record.broker_order_id is None:
                raise ReconciliationSnapshotError("active local order has no durable broker order id")
            try:
                order_id = int(record.broker_order_id)
            except (TypeError, ValueError) as exc:
                raise ReconciliationSnapshotError("active local order has an invalid broker order id") from exc
            expected_ids.add(order_id)
        if set(broker_orders) != expected_ids:
            raise ReconciliationSnapshotError(
                "broker open-order set differs from the durable active-order set; execution history is required"
            )
        return {"count": len(broker_orders)}

    def _reconcile_positions(self, snapshot: dict[str, Any]) -> dict[str, int]:
        expected: dict[tuple[int, str, str], Decimal] = {}
        with closing(sqlite3.connect(self.ledger.path)) as db:
            rows = db.execute(
                "SELECT f.quantity,f.intent_id,i.body_json,b.contract_id,b.symbol,b.security_type,b.currency "
                "FROM fill f JOIN order_intent i ON i.intent_id=f.intent_id "
                "LEFT JOIN broker_order_binding b ON b.intent_id=f.intent_id "
                "WHERE f.broker='IBKR' AND f.account_ref=? AND f.environment='paper'",
                (self.account_ref,),
            ).fetchall()
        for quantity, _intent_id, body_json, contract_id, symbol, security_type, currency in rows:
            if contract_id is None or not symbol or security_type != "STK" or currency != "USD":
                raise ReconciliationSnapshotError("durable fill lacks a verified stock contract mapping")
            body = json.loads(body_json)
            delta = Decimal(quantity) * (1 if body["side"] == "BUY" else -1)
            key = (int(contract_id), str(symbol), str(currency))
            expected[key] = expected.get(key, Decimal("0")) + delta
        expected = {key: value for key, value in expected.items() if value != 0}
        actual: dict[tuple[int, str, str], Decimal] = {}
        for row in snapshot["positions"]:
            key = (row["contract_id"], row["symbol"], row["currency"])
            if key in actual:
                raise ReconciliationSnapshotError("broker position snapshot repeats a contract")
            actual[key] = Decimal(row["quantity"])
        actual = {key: value for key, value in actual.items() if value != 0}
        if actual != expected:
            raise ReconciliationSnapshotError("broker positions differ from durable execution fills")
        return {"count": len(actual)}

    def _known_order_ids(self) -> set[int]:
        with closing(sqlite3.connect(self.ledger.path)) as db:
            rows = db.execute(
                "SELECT broker_order_id FROM broker_order_binding WHERE account_ref=? AND environment='paper'",
                (self.account_ref,),
            ).fetchall()
        return {int(row[0]) for row in rows if row[0] is not None}
