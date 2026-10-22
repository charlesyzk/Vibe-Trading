from __future__ import annotations

import unittest
from decimal import Decimal
import json
from queue import Queue
from threading import Event
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sqlite3

from ibapi.client import EClient
from ibapi.order_cancel import OrderCancel
from ibkr_runtime.callbacks import CallbackEvent, IbapiCallbackAdapter, SQLiteCallbackSpool
from ibkr_runtime.cancel_transport import IbapiCancelTransport
from ibkr_runtime.order_submitter import DispatchDenied, IbapiOrderSubmitter


class IbapiCallbackAdapterTests(unittest.TestCase):
    def test_official_sdk_error_callback_preserves_full_primitive_payload(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=4)
        adapter = IbapiCallbackAdapter(events, durable_sink=lambda _event: None)

        # This is the installed official EWrapper callback shape, including
        # the newer advanced-order-reject JSON parameter.
        adapter.error(41, 1730000000, 201, "Order rejected", '{"reason":"fixture"}')

        event = events.get_nowait()
        self.assertEqual(event.kind, "error")
        self.assertEqual(dict(event.payload), {
            "advanced_reject": '{"reason":"fixture"}',
            "error_code": 201,
            "error_time": 1730000000,
            "message": "Order rejected",
            "request_id": 41,
        })

    def test_reconciliation_callbacks_have_snapshot_identity_and_three_end_markers(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=16)
        adapter = IbapiCallbackAdapter(events, durable_sink=lambda _event: None)
        adapter.begin_reconciliation_snapshot(1900000001)
        adapter.accountSummary(1900000001, "DU-TEST", "NetLiquidation", "1000", "USD")
        adapter.position("DU-TEST", SimpleNamespace(symbol="AAPL", conId=1, currency="USD"),
                         Decimal("2"), 100.0)
        adapter.openOrder(7, SimpleNamespace(symbol="AAPL", conId=1, secType="STK", currency="USD"),
                          SimpleNamespace(permId=0, action="BUY", totalQuantity=2, orderType="MKT",
                                          tif="DAY"),
                          SimpleNamespace(status="Submitted", warningText=""))
        adapter.accountSummaryEnd(1900000001)
        adapter.positionEnd()
        adapter.openOrderEnd()

        batch = [events.get_nowait() for _ in range(events.qsize())]
        self.assertEqual([event.kind for event in batch], [
            "account_summary", "position", "open_order", "account_summary_end",
            "positions_end", "open_orders_end",
        ])
        for event in batch:
            self.assertEqual(dict(event.payload)["snapshot_id"], 1900000001)
        # All three markers release the in-flight slot for the next request.
        adapter.begin_reconciliation_snapshot(1900000002)

    def test_wrong_account_summary_end_latches_overflow_and_does_not_complete_snapshot(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=4)
        overflowed = Event()
        adapter = IbapiCallbackAdapter(events, overflowed, durable_sink=lambda _event: None)
        adapter.begin_reconciliation_snapshot(1900000001)
        adapter.accountSummaryEnd(1900000002)
        self.assertTrue(overflowed.is_set())
        self.assertEqual(events.qsize(), 0)
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            adapter.begin_reconciliation_snapshot(1900000003)

    def test_sqlite_spool_persists_and_deduplicates_exact_callback(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "callbacks.sqlite"
            spool = SQLiteCallbackSpool(path)
            event = CallbackEvent.create("next_valid_id", order_id=7)

            spool.append(event)
            SQLiteCallbackSpool(path).append(event)

            with sqlite3.connect(path) as db:
                rows = db.execute(
                    "SELECT kind,payload_json,processed FROM ibkr_callback_spool"
                ).fetchall()
            self.assertEqual(rows, [("next_valid_id", json.dumps({"order_id": 7}, separators=(",", ":")), 0)])

    def test_spool_returns_ordered_pending_events_and_acknowledges_after_consumer_commit(self) -> None:
        with TemporaryDirectory() as directory:
            spool = SQLiteCallbackSpool(Path(directory) / "callbacks.sqlite")
            first = CallbackEvent.create("next_valid_id", order_id=7)
            second = CallbackEvent.create("order_status", order_id=7, status="Submitted")
            spool.append(first)
            spool.append(second)

            batch = spool.pending(limit=1)
            self.assertEqual(len(batch), 1)
            first_sequence, first_event = batch[0]
            self.assertEqual(first_event, first)
            self.assertEqual(spool.pending(limit=1)[0], batch[0])
            spool.mark_processed(first_sequence)
            self.assertEqual(spool.pending(), [(2, second)])
            spool.mark_processed(2)
            self.assertEqual(spool.pending(), [])

    def test_durable_sink_runs_before_in_memory_queue(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=2)
        order = []

        def persist(event):
            order.append(("persist", event.kind))

        adapter = IbapiCallbackAdapter(events, durable_sink=persist)
        original_put = events.put_nowait

        def observed_put(event):
            order.append(("queue", event.kind))
            original_put(event)

        events.put_nowait = observed_put
        adapter.nextValidId(7)

        self.assertEqual(order, [("persist", "next_valid_id"), ("queue", "next_valid_id")])

    def test_durable_sink_failure_latches_overflow_and_does_not_queue(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=2)
        overflowed = Event()

        def fail(_event):
            raise OSError("durable spool unavailable")

        adapter = IbapiCallbackAdapter(events, overflowed, durable_sink=fail)
        adapter.nextValidId(7)

        self.assertTrue(overflowed.is_set())
        self.assertEqual(events.qsize(), 0)

    def test_order_status_becomes_immutable_primitive_event(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=4)
        adapter = IbapiCallbackAdapter(events, durable_sink=lambda _event: None)

        adapter.orderStatus(7, "Submitted", Decimal("0"), Decimal("4"), 0.0, 90, 0, 0.0, 3, "", 0.0)

        event = events.get_nowait()
        self.assertEqual(event.kind, "order_status")
        self.assertEqual(
            dict(event.payload),
            {
                "average_fill_price": "0.0",
                "client_id": 3,
                "filled": "0",
                "last_fill_price": "0.0",
                "market_cap_price": "0.0",
                "order_id": 7,
                "parent_id": 0,
                "permanent_id": 90,
                "remaining": "4",
                "status": "Submitted",
                "why_held": "",
            },
        )

    def test_queue_overflow_latches_fail_closed_and_stops_publishing(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=1)
        overflowed = Event()
        adapter = IbapiCallbackAdapter(events, overflowed, durable_sink=lambda _event: None)

        adapter.nextValidId(7)
        adapter.nextValidId(8)
        adapter.nextValidId(9)

        self.assertTrue(overflowed.is_set())
        self.assertEqual(events.qsize(), 1)
        self.assertEqual(dict(events.get_nowait().payload), {"order_id": 7})

    def test_execution_callback_does_not_expose_sdk_objects(self) -> None:
        events: Queue[CallbackEvent] = Queue(maxsize=2)
        adapter = IbapiCallbackAdapter(events, durable_sink=lambda _event: None)
        contract = SimpleNamespace(symbol="XYZ", conId=1234, secType="STK", currency="USD")
        execution = SimpleNamespace(
            execId="exec-1", orderId=7, acctNumber="PAPER", side="BOT",
            shares=Decimal("2"), price=12.5, time="20261020  10:00:00",
        )

        adapter.execDetails(2, contract, execution)

        event = events.get_nowait()
        self.assertEqual(event.kind, "execution")
        payload = dict(event.payload)
        self.assertEqual(payload["execution_id"], "exec-1")
        self.assertEqual(payload["shares"], "2")
        self.assertEqual(payload["contract_id"], 1234)
        self.assertEqual(payload["symbol"], "XYZ")
        self.assertEqual(payload["security_type"], "STK")
        self.assertEqual(payload["currency"], "USD")
        self.assertTrue(all(type(value) in {str, int, bool} or value is None for value in payload.values()))


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.started = False

    def start_send(self, intent_id: str, *, attempt_id: str, client_order_id: str) -> None:
        if self.started:
            raise RuntimeError("submission already started")
        self.events.append("SEND_STARTED_COMMITTED")
        self.started = True

    def mark_unknown(self, intent_id: str) -> None:
        self.events.append("UNKNOWN_COMMITTED")


class IbapiOrderSubmitterTests(unittest.TestCase):
    def test_rejected_pre_dispatch_check_has_no_ledger_or_client_side_effect(self) -> None:
        ledger = FakeLedger()
        client = Mock(spec=EClient)
        submitter = IbapiOrderSubmitter(client=client, ledger=ledger, pre_dispatch_check=lambda _: False)

        with self.assertRaises(DispatchDenied):
            submitter.submit(
                intent_id="intent-1", attempt_id="attempt-1", client_order_id="client-1",
                broker_order_id=1, contract=object(), order=object(),
            )

        self.assertEqual(ledger.events, [])
        self.assertFalse(ledger.started)
        client.placeOrder.assert_not_called()
        client.connect.assert_not_called()


class IbapiCancelTransportTests(unittest.TestCase):
    def test_cancels_only_the_supplied_positive_order_id_with_sdk_cancel_object(self) -> None:
        client = Mock(spec=EClient)
        transport = IbapiCancelTransport(client=client)

        transport(41)

        client.cancelOrder.assert_called_once()
        order_id, payload = client.cancelOrder.call_args.args
        self.assertEqual(order_id, 41)
        self.assertIsInstance(payload, OrderCancel)
        self.assertEqual(payload.manualOrderCancelTime, "")

    def test_invalid_order_id_has_no_sdk_side_effect(self) -> None:
        client = Mock(spec=EClient)
        transport = IbapiCancelTransport(client=client)

        for order_id in (0, -1, True, "41"):
            with self.subTest(order_id=order_id), self.assertRaises(ValueError):
                transport(order_id)

        client.cancelOrder.assert_not_called()

    def test_send_started_commits_before_sdk_call_and_uncertain_send_never_retries(self) -> None:
        ledger = FakeLedger()
        client = Mock(spec=EClient)

        def fake_send(*args):
            ledger.events.append("PLACE_ORDER")
            raise OSError("fake transport outcome unknown")

        client.placeOrder.side_effect = fake_send
        submitter = IbapiOrderSubmitter(client=client, ledger=ledger, pre_dispatch_check=lambda _: True)

        with self.assertRaisesRegex(OSError, "outcome unknown"):
            submitter.submit(
                intent_id="intent-1", attempt_id="attempt-1", client_order_id="client-1",
                broker_order_id=1, contract=object(), order=object(),
            )

        self.assertEqual(ledger.events, ["SEND_STARTED_COMMITTED", "PLACE_ORDER", "UNKNOWN_COMMITTED"])
        with self.assertRaisesRegex(RuntimeError, "already started"):
            submitter.submit(
                intent_id="intent-1", attempt_id="attempt-2", client_order_id="client-2",
                broker_order_id=2, contract=object(), order=object(),
            )
        self.assertEqual(ledger.events.count("PLACE_ORDER"), 1)
        client.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
