from __future__ import annotations

import sqlite3
import threading
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from ibkr_runtime.lifecycle import ClientLifecycle, IbapiClientOwner, official_client_factory


class FakeClient:
    def __init__(self, wrapper, *, block_run: bool = False, block_connect: bool = False,
                 run_error: BaseException | None = None) -> None:
        self.wrapper = wrapper
        self.block_run = block_run
        self.block_connect = block_connect
        self.run_error = run_error
        self.connected = threading.Event()
        self.release_connect = threading.Event()
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.calls: list[tuple] = []

    def connect(self, host: str, port: int, *, clientId: int) -> None:
        self.calls.append(("connect", host, port, clientId))
        self.connected.set()
        if self.block_connect:
            self.release_connect.wait()

    def run(self) -> None:
        self.calls.append(("run",))
        self.run_started.set()
        if self.run_error is not None:
            raise self.run_error
        if self.block_run:
            self.release_run.wait()

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        self.release_connect.set()
        self.release_run.set()

    def reqAccountSummary(self, request_id, group, tags):
        self.calls.append(("reqAccountSummary", request_id, group, tags))

    def reqPositions(self):
        self.calls.append(("reqPositions",))

    def reqAllOpenOrders(self):
        self.calls.append(("reqAllOpenOrders",))

    def reqIds(self, count):
        self.calls.append(("reqIds", count))

    def cancelAccountSummary(self, request_id):
        self.calls.append(("cancelAccountSummary", request_id))

    def cancelPositions(self):
        self.calls.append(("cancelPositions",))


class IbapiClientOwnerTests(unittest.TestCase):
    def test_order_id_refresh_requires_ready_running_client_and_uses_reqids(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(None, block_run=True)
            owner = IbapiClientOwner(
                str(Path(directory) / "callbacks.sqlite"), client_factory=lambda _: client,
            )
            with self.assertRaisesRegex(RuntimeError, "not running"):
                owner.request_order_ids()
            owner.start("localhost", 4002, 4)
            self.assertTrue(client.run_started.wait(1))
            with self.assertRaisesRegex(RuntimeError, "not API-ready"):
                owner.request_order_ids()
            owner.adapter.nextValidId(81)
            owner.adapter.managedAccounts("DU-TEST")
            owner.request_order_ids()
            self.assertEqual(client.calls[-1], ("reqIds", -1))
            owner.stop(timeout=1)

    def test_reconciliation_request_issues_account_position_and_all_order_queries(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(None, block_run=True)
            owner = IbapiClientOwner(
                str(Path(directory) / "callbacks.sqlite"), client_factory=lambda _: client,
            )
            with self.assertRaisesRegex(RuntimeError, "not running"):
                owner.request_reconciliation_snapshot(1900000001)
            owner.start("localhost", 4002, 4)
            self.assertTrue(client.run_started.wait(1))
            snapshot_id = 1_900_000_001
            self.assertEqual(owner.request_reconciliation_snapshot(snapshot_id), snapshot_id)
            self.assertEqual(client.calls[-3:], [
                ("reqAccountSummary", snapshot_id, "All", "NetLiquidation,TotalCashValue,AvailableFunds"),
                ("reqPositions",),
                ("reqAllOpenOrders",),
            ])
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                owner.request_reconciliation_snapshot(snapshot_id + 1)
            owner.adapter.accountSummaryEnd(snapshot_id)
            self.assertIn(("cancelAccountSummary", snapshot_id), client.calls)
            owner.adapter.positionEnd()
            self.assertIn(("cancelPositions",), client.calls)
            owner.adapter.openOrderEnd()
            self.assertEqual(owner.request_reconciliation_snapshot(snapshot_id + 1), snapshot_id + 1)
            owner.stop(timeout=1)

    def test_construct_does_not_connect_and_explicit_start_runs_dedicated_loop(self) -> None:
        with TemporaryDirectory() as directory:
            clients: list[FakeClient] = []

            def factory(wrapper):
                client = FakeClient(wrapper, block_run=True)
                clients.append(client)
                return client

            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=factory)
            client = clients[0]
            self.assertEqual(owner.state, ClientLifecycle.NEW)
            self.assertEqual(client.calls, [])
            self.assertIs(owner.adapter, client.wrapper)

            owner.start("127.0.0.1", 4002, 23)
            self.assertTrue(client.run_started.wait(1))
            self.assertEqual(client.calls[0], ("connect", "127.0.0.1", 4002, 23))
            self.assertEqual(client.calls[1], ("run",))
            self.assertIsNotNone(owner.thread)
            self.assertTrue(owner.thread.daemon)
            self.assertEqual(owner.state, ClientLifecycle.RUNNING)

            owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)
            self.assertEqual(client.calls.count(("disconnect",)), 1)

    def test_callback_queue_and_durable_spool_are_exposed_on_owner(self) -> None:
        with TemporaryDirectory() as directory:
            owner = IbapiClientOwner(
                str(Path(directory) / "callbacks.sqlite"),
                client_factory=lambda wrapper: FakeClient(wrapper),
            )

            owner.adapter.nextValidId(81)

            event = owner.events.get_nowait()
            self.assertEqual(event.kind, "next_valid_id")
            self.assertEqual(dict(event.payload), {"order_id": 81})
            with sqlite3.connect(owner.spool.path) as db:
                rows = db.execute(
                    "SELECT kind,payload_json,processed FROM ibkr_callback_spool"
                ).fetchall()
            self.assertEqual(rows, [("next_valid_id", '{"order_id":81}', 0)])

    def test_stop_times_out_if_sdk_run_thread_does_not_exit(self) -> None:
        with TemporaryDirectory() as directory:
            clients: list[FakeClient] = []

            def factory(wrapper):
                client = FakeClient(wrapper, block_run=True)
                clients.append(client)
                return client

            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=factory)
            client = clients[0]
            # Model a broken SDK/client where disconnect does not unblock run.
            client.disconnect = lambda: client.calls.append(("disconnect",))
            owner.start("localhost", 4002, 0)
            self.assertTrue(client.run_started.wait(1))

            with self.assertRaises(TimeoutError):
                owner.stop(timeout=0.01)

            self.assertEqual(owner.state, ClientLifecycle.STOPPING)
            self.assertTrue(owner.thread.is_alive())
            client.release_run.set()
            owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)

    def test_disconnect_failure_is_not_reported_as_stopped_and_can_be_retried(self) -> None:
        with TemporaryDirectory() as directory:
            clients: list[FakeClient] = []

            def factory(wrapper):
                client = FakeClient(wrapper, block_run=True)
                clients.append(client)
                return client

            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=factory)
            client = clients[0]
            original_disconnect = client.disconnect
            fail_once = True

            def disconnect_with_one_failure():
                nonlocal fail_once
                original_disconnect()
                if fail_once:
                    fail_once = False
                    raise OSError("simulated disconnect failure")

            client.disconnect = disconnect_with_one_failure
            owner.start("localhost", 4002, 5)
            self.assertTrue(client.run_started.wait(1))

            with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
                owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.FAILED)

            owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)

    def test_run_loop_exception_remains_failed_until_host_confirms_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(None, run_error=OSError("simulated SDK loop failure"))
            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=lambda _: client)
            owner.start("localhost", 4002, 5)
            self.assertTrue(client.run_started.wait(1))
            self.assertEqual(owner.state, ClientLifecycle.FAILED)
            with self.assertRaisesRegex(RuntimeError, "reconciliation is required"):
                owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.FAILED)
            self.assertIsInstance(owner.run_error, OSError)
            owner.confirm_reconciled_stop()
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)
            self.assertIsInstance(owner.run_error, OSError)

    def test_unexpected_run_loop_return_is_not_clean_shutdown(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(None)
            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=lambda _: client)
            owner.start("localhost", 4002, 5)
            self.assertTrue(client.run_started.wait(1))
            owner.thread.join(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.FAILED)
            with self.assertRaisesRegex(RuntimeError, "reconciliation is required"):
                owner.stop(timeout=1)
            owner.confirm_reconciled_stop()
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)

    def test_start_returns_while_connect_is_still_blocked_and_stop_is_bounded(self) -> None:
        with TemporaryDirectory() as directory:
            client = FakeClient(None, block_connect=True)
            owner = IbapiClientOwner(str(Path(directory) / "callbacks.sqlite"), client_factory=lambda _: client)
            owner.start("localhost", 4002, 5)
            self.assertEqual(owner.state, ClientLifecycle.STARTING)
            client.disconnect = lambda: client.calls.append(("disconnect",))
            with self.assertRaises(TimeoutError):
                owner.stop(timeout=0.01)
            self.assertEqual(owner.state, ClientLifecycle.STOPPING)
            client.release_connect.set()
            owner.stop(timeout=1)
            self.assertEqual(owner.state, ClientLifecycle.STOPPED)

    def test_invalid_endpoint_is_rejected_before_connect(self) -> None:
        with TemporaryDirectory() as directory:
            clients: list[FakeClient] = []
            owner = IbapiClientOwner(
                str(Path(directory) / "callbacks.sqlite"),
                client_factory=lambda wrapper: clients.append(FakeClient(wrapper)) or clients[-1],
            )
            for endpoint in (("", 4002, 1), ("localhost", True, 1), ("localhost", 4002, -1)):
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    owner.start(*endpoint)
            self.assertEqual(clients[0].calls, [])
            for timeout in (float("nan"), float("inf"), True):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    owner.stop(timeout=timeout)

    def test_official_factory_constructs_without_connecting(self) -> None:
        from ibapi.client import EClient

        client = official_client_factory(object())

        self.assertIsInstance(client, EClient)
        self.assertFalse(client.isConnected())


if __name__ == "__main__":
    unittest.main()
