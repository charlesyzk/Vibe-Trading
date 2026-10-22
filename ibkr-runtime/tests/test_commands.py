from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ibkr_runtime.commands import execute
from ibkr_runtime.lifecycle import ClientLifecycle


def _order_payload() -> dict[str, object]:
    return {
        "broker_order_id": 73,
        "environment": "paper",
        "contract": {
            "con_id": 265598,
            "symbol": "AAPL",
            "security_type": "STK",
            "exchange": "SMART",
            "primary_exchange": "NASDAQ",
            "currency": "USD",
        },
        "order": {
            "action": "BUY",
            "quantity": 2,
            "order_type": "MKT",
            "time_in_force": "DAY",
            "account_ref": "DU-TEST",
            "client_order_id": "attempt-test-73",
            "outside_rth": False,
            "transmit": True,
        },
    }


class IsolatedSdkCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.owner = SimpleNamespace(state=ClientLifecycle.RUNNING, client=self.client)

    def test_place_order_constructs_only_constrained_official_sdk_objects(self) -> None:
        result = execute(self.owner, "place_order", _order_payload())
        self.assertEqual(result, {"broker_order_id": 73, "operation": "place_requested"})
        self.client.placeOrder.assert_called_once()
        broker_order_id, contract, order = self.client.placeOrder.call_args.args
        self.assertEqual(broker_order_id, 73)
        self.assertEqual((contract.conId, contract.symbol, contract.secType, contract.exchange,
                          contract.primaryExchange, contract.currency),
                         (265598, "AAPL", "STK", "SMART", "NASDAQ", "USD"))
        self.assertEqual((order.action, order.totalQuantity, order.orderType, order.tif,
                          order.account, order.orderRef, order.outsideRth, order.transmit),
                         ("BUY", 2, "MKT", "DAY", "DU-TEST", "attempt-test-73", False, True))

    def test_rejects_live_or_unsupported_order_before_sdk_side_effect(self) -> None:
        for mutate in (
            lambda payload: payload.update(environment="live"),
            lambda payload: payload["order"].update(quantity=True),
            lambda payload: payload["order"].update(order_type="LMT"),
            lambda payload: payload["contract"].update(currency="CAD"),
        ):
            payload = _order_payload()
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                execute(self.owner, "place_order", payload)
        self.client.placeOrder.assert_not_called()

    def test_cancel_uses_only_exact_positive_order_id(self) -> None:
        result = execute(self.owner, "cancel_order", {"broker_order_id": 73, "environment": "paper"})
        self.assertEqual(result, {"broker_order_id": 73, "operation": "cancel_requested"})
        self.client.cancelOrder.assert_called_once()
        order_id, cancel = self.client.cancelOrder.call_args.args
        self.assertEqual(order_id, 73)
        self.assertEqual(type(cancel).__name__, "OrderCancel")
        for payload in (
            {"broker_order_id": 0, "environment": "paper"},
            {"broker_order_id": 73, "environment": "live"},
            {"broker_order_id": 73, "environment": "paper", "account_ref": "DU-TEST"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                execute(self.owner, "cancel_order", payload)
        self.assertEqual(self.client.cancelOrder.call_count, 1)

    def test_order_and_cancel_are_refused_outside_running_state(self) -> None:
        self.owner.state = ClientLifecycle.NEW
        with self.assertRaisesRegex(RuntimeError, "not running"):
            execute(self.owner, "place_order", _order_payload())
        with self.assertRaisesRegex(RuntimeError, "not running"):
            execute(self.owner, "cancel_order", {"broker_order_id": 73, "environment": "paper"})
        self.client.placeOrder.assert_not_called()
        self.client.cancelOrder.assert_not_called()

    def test_reconciliation_command_requires_running_client_and_delegates_to_owner(self) -> None:
        self.owner.request_reconciliation_snapshot = Mock(return_value=1900000001)
        self.assertEqual(execute(self.owner, "request_reconciliation", {"snapshot_id": 1900000001}),
                         {"snapshot_id": 1900000001})
        self.owner.request_reconciliation_snapshot.assert_called_once_with(1900000001)
        self.owner.state = ClientLifecycle.NEW
        with self.assertRaisesRegex(RuntimeError, "not running"):
            execute(self.owner, "request_reconciliation", {"snapshot_id": 1900000001})

    def test_order_id_refresh_delegates_to_owner_and_requires_running_client(self) -> None:
        self.owner.request_order_ids = Mock()
        self.assertEqual(execute(self.owner, "request_order_ids", {}), {"requested": True})
        self.owner.request_order_ids.assert_called_once_with()
        self.owner.state = ClientLifecycle.NEW
        with self.assertRaisesRegex(RuntimeError, "not running"):
            execute(self.owner, "request_order_ids", {})


if __name__ == "__main__":
    unittest.main()
