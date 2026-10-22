from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.execution.ibkr_runtime_process import (
    IbkrSubprocessCancelTransport,
    IbkrSubprocessPaperTransport,
    IbkrRuntimeHealthMonitor,
    IbkrRuntimeProcess,
    RuntimeIpcError,
    RuntimeProcessError,
    validate_command,
)


class RuntimeIpcTests(unittest.TestCase):
    def test_health_monitor_keeps_dispatch_readiness_separate_and_pauses_on_overflow(self) -> None:
        statuses = [
            {"state": "RUNNING", "socket_connected": True, "sdk_api_ready": True,
             "ready_for_dispatch": False, "callback_overflow": False, "run_error": None},
            {"state": "RUNNING", "socket_connected": True, "sdk_api_ready": True,
             "ready_for_dispatch": False, "callback_overflow": True, "run_error": None},
        ]
        paused = []
        monitor = IbkrRuntimeHealthMonitor(
            status_reader=lambda: statuses.pop(0), pause=paused.append,
        )

        self.assertTrue(monitor.check_once())
        self.assertFalse(monitor.paused)
        self.assertFalse(monitor.check_once())
        self.assertTrue(monitor.paused)
        self.assertEqual(paused, ["ibkr_callback_overflow_or_unknown"])

    def test_health_monitor_pauses_when_status_channel_fails(self) -> None:
        paused = []
        monitor = IbkrRuntimeHealthMonitor(
            status_reader=lambda: (_ for _ in ()).throw(TimeoutError("ipc timeout")),
            pause=paused.append,
        )

        self.assertFalse(monitor.check_once())
        self.assertTrue(monitor.paused)
        self.assertIsInstance(monitor.failure, TimeoutError)
        self.assertEqual(paused, ["ibkr_runtime_status_unavailable"])

    def test_rejects_unknown_fields_and_invalid_scope_parameters(self) -> None:
        validate_command("status", {})
        validate_command("request_reconciliation", {"snapshot_id": 1900000001})
        validate_command("request_order_ids", {})
        for command, payload in (
            ("execute_code", {}),
            ("request_reconciliation", {}),
            ("request_reconciliation", {"snapshot_id": True}),
            ("request_order_ids", {"snapshot_id": 1}),
            (["status"], {}),
            ("start", {"host": "localhost", "port": 4002, "client_id": 0, "account": "DU123"}),
            ("start", {"host": "localhost", "port": True, "client_id": 0}),
            ("status", {"actor": "user"}),
        ):
            with self.subTest(command=command, payload=payload), self.assertRaises(RuntimeIpcError):
                validate_command(command, payload)

    def test_order_id_request_result_is_exact(self) -> None:
        runtime = object.__new__(IbkrRuntimeProcess)
        runtime._failed = False
        runtime.request = Mock(return_value={"requested": True})
        self.assertIsNone(runtime.request_order_ids())
        runtime.request.assert_called_once_with("request_order_ids")
        for result in ({}, {"requested": False}, {"requested": True, "order_id": 12}):
            runtime.request = Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(RuntimeIpcError):
                runtime.request_order_ids()
            self.assertTrue(runtime._failed)
            runtime._failed = False

    def test_reconciliation_request_result_requires_a_positive_snapshot_id(self) -> None:
        runtime = object.__new__(IbkrRuntimeProcess)
        runtime._failed = False
        runtime.request = Mock(return_value={"snapshot_id": 1900000001})
        self.assertEqual(runtime.request_reconciliation(1900000001), 1900000001)
        runtime.request.assert_called_once_with("request_reconciliation", snapshot_id=1900000001)
        for result in ({}, {"snapshot_id": 0}, {"snapshot_id": True},
                       {"snapshot_id": 10, "ready": True}):
            runtime.request = Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(RuntimeIpcError):
                runtime.request_reconciliation(1900000001)
            self.assertTrue(runtime._failed)
            runtime._failed = False
        runtime.request = Mock(return_value={"snapshot_id": 1900000002})
        with self.assertRaises(RuntimeIpcError):
            runtime.request_reconciliation(1900000001)

    def test_transport_serializes_only_matching_frozen_paper_intent(self) -> None:
        process = Mock()
        intent = SimpleNamespace(
            broker="IBKR", environment="paper", instrument_id="AAPL", side="BUY",
            integer_qty=2, order_type="market", time_in_force="DAY", account_ref="DU-TEST",
        )
        contract = {
            "con_id": 265598, "symbol": "AAPL", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        }
        transport = IbkrSubprocessPaperTransport(
            process=process, intent_loader=lambda _intent_id: intent,
            contract_loader=lambda _instrument: contract,
        )
        from src.bt_runtime.brokers.proxy import BrokerOrderCommand

        transport(BrokerOrderCommand(11, "AAPL", "BUY", 2), intent_id="intent-1",
                  broker_order_id=73, attempt_id="attempt-1", client_order_id="client-1")
        process.place_order.assert_called_once_with(
            broker_order_id=73,
            contract=contract,
            order={"action": "BUY", "quantity": 2, "order_type": "MKT", "time_in_force": "DAY",
                   "account_ref": "DU-TEST", "client_order_id": "client-1", "outside_rth": False,
                   "transmit": True},
        )

        process.reset_mock()
        with self.assertRaises(RuntimeIpcError):
            transport(BrokerOrderCommand(12, "AAPL", "BUY", 3), intent_id="intent-1",
                      broker_order_id=74, attempt_id="attempt-2", client_order_id="client-2")
        process.place_order.assert_not_called()

    def test_contract_mapping_must_match_frozen_instrument_before_child_call(self) -> None:
        process = Mock()
        intent = SimpleNamespace(
            broker="IBKR", environment="paper", instrument_id="AAPL", side="BUY",
            integer_qty=2, order_type="market", time_in_force="DAY", account_ref="DU-TEST",
        )
        contract = {
            "con_id": 265598, "symbol": "MSFT", "security_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        }
        transport = IbkrSubprocessPaperTransport(
            process=process, intent_loader=lambda _intent_id: intent,
            contract_loader=lambda _instrument: contract,
        )
        from src.bt_runtime.brokers.proxy import BrokerOrderCommand

        with self.assertRaisesRegex(RuntimeIpcError, "contract mapping"):
            transport(BrokerOrderCommand(11, "AAPL", "BUY", 2), intent_id="intent-1",
                      broker_order_id=73, attempt_id="attempt-1", client_order_id="client-1")
        process.place_order.assert_not_called()

    def test_cancel_transport_accepts_only_positive_exact_order_id(self) -> None:
        process = Mock()
        cancel = IbkrSubprocessCancelTransport(process=process)
        cancel(73)
        process.cancel_order.assert_called_once_with(73)
        process.reset_mock()
        for invalid in (0, -1, True, "73"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeIpcError):
                cancel(invalid)
        process.cancel_order.assert_not_called()

    def test_isolated_subprocess_starts_disconnected_and_shuts_down(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        runtime_root = repository / "ibkr-runtime"
        python = runtime_root / ".venv" / "bin" / "python"
        with tempfile.TemporaryDirectory() as directory:
            runtime = IbkrRuntimeProcess(
                python=python, cwd=runtime_root, spool_path=Path(directory) / "spool.sqlite",
            )
            status = runtime.start_process()
            self.assertEqual(status["state"], "NEW")
            self.assertFalse(status["socket_connected"])
            self.assertFalse(status["sdk_api_ready"])
            self.assertFalse(status["ready_for_dispatch"])
            self.assertEqual(runtime.request("status")["state"], "NEW")
            runtime.close()
            with self.assertRaises(RuntimeProcessError):
                runtime.request("status")


if __name__ == "__main__":
    unittest.main()
