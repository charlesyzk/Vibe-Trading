from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace

from ibapi.contract import Contract
from ibapi.order import Order

from ibkr_runtime.order_factory import IbapiOrderFactory, VerifiedStockContract
from ibkr_runtime.paper_transport import IbapiPaperTransport


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def intent(**changes):
    values = {
        "broker": "IBKR",
        "environment": "paper",
        "account_ref": "DU-PAPER-1",
        "instrument_id": "us:NASDAQ:XYZ",
        "side": "BUY",
        "integer_qty": 3,
        "order_type": "market",
        "time_in_force": "DAY",
    }
    values.update(changes)
    return SimpleNamespace(**values)


class IbapiOrderFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = VerifiedStockContract(
            instrument_id="us:NASDAQ:XYZ",
            con_id=123456,
            symbol="XYZ",
            exchange="SMART",
            primary_exchange="NASDAQ",
            currency="USD",
            verified_at=NOW - timedelta(days=1),
            verification_source="operator-reviewed fixture",
        )
        self.factory = IbapiOrderFactory(
            contracts={self.mapping.instrument_id: self.mapping},
            account_ref="DU-PAPER-1",
        )

    def test_builds_official_sdk_market_day_stock_objects(self) -> None:
        contract, order = self.factory.build(intent(), now=NOW)

        self.assertIsInstance(contract, Contract)
        self.assertEqual(
            (contract.conId, contract.symbol, contract.secType, contract.exchange,
             contract.primaryExchange, contract.currency),
            (123456, "XYZ", "STK", "SMART", "NASDAQ", "USD"),
        )
        self.assertIsInstance(order, Order)
        self.assertEqual(
            (order.action, order.totalQuantity, order.orderType, order.tif, order.account,
             order.outsideRth, order.transmit),
            ("BUY", 3, "MKT", "DAY", "DU-PAPER-1", False, True),
        )

    def test_rejects_non_paper_wrong_account_and_unsupported_order_shape(self) -> None:
        for bad_intent in (
            intent(environment="live"),
            intent(account_ref="DU-PAPER-2"),
            intent(order_type="limit"),
            intent(time_in_force="GTC"),
            intent(side="SHORT"),
            intent(integer_qty=1.5),
            intent(integer_qty=0),
            intent(instrument_id="unknown"),
        ):
            with self.subTest(bad_intent=bad_intent):
                with self.assertRaises(ValueError):
                    self.factory.build(bad_intent, now=NOW)

    def test_rejects_stale_or_future_verified_contract(self) -> None:
        stale_factory = IbapiOrderFactory(
            contracts={self.mapping.instrument_id: replace(
                self.mapping, verified_at=NOW - timedelta(days=31)
            )},
            account_ref="DU-PAPER-1",
        )
        with self.assertRaisesRegex(ValueError, "stale or from the future"):
            stale_factory.build(intent(), now=NOW)

        future_factory = IbapiOrderFactory(
            contracts={self.mapping.instrument_id: replace(
                self.mapping, verified_at=NOW + timedelta(seconds=1)
            )},
            account_ref="DU-PAPER-1",
        )
        with self.assertRaisesRegex(ValueError, "stale or from the future"):
            future_factory.build(intent(), now=NOW)

    def test_rejects_non_stock_or_non_usd_mapping(self) -> None:
        for changes in ({"security_type": "OPT"}, {"currency": "CAD"}, {"exchange": "NYSE"}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "SMART-routed USD STK"):
                    values = {
                        "instrument_id": "us:NASDAQ:XYZ", "con_id": 123, "symbol": "XYZ",
                        "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
                        "verified_at": NOW, "verification_source": "test",
                    }
                    values.update(changes)
                    VerifiedStockContract(**values)


class IbapiPaperTransportTests(unittest.TestCase):
    def test_native_command_must_match_frozen_intent_before_submit(self) -> None:
        frozen = intent()
        submitter = SimpleNamespace(submit_precommitted=lambda **kwargs: self.fail("must not submit"))
        transport = IbapiPaperTransport(
            intent_loader=lambda _intent_id: frozen,
            order_factory=SimpleNamespace(build=lambda _intent: self.fail("must not build")),
            submitter=submitter,
        )
        command = SimpleNamespace(
            instrument="us:NASDAQ:XYZ", side="BUY", quantity=4,
            order_type="market", time_in_force="DAY",
        )

        with self.assertRaisesRegex(ValueError, "differs from the frozen intent"):
            transport(
                command, intent_id="intent-1", broker_order_id=7,
                attempt_id="attempt-1", client_order_id="client-1",
            )

    def test_verified_factory_objects_reach_precommitted_sdk_boundary(self) -> None:
        frozen = intent()
        factory = IbapiOrderFactory(
            contracts={frozen.instrument_id: VerifiedStockContract(
                instrument_id=frozen.instrument_id, con_id=123, symbol="XYZ",
                exchange="SMART", primary_exchange="NASDAQ", currency="USD",
                verified_at=datetime.now(timezone.utc), verification_source="operator-reviewed fixture",
            )},
            account_ref="DU-PAPER-1",
        )
        calls = []
        transport = IbapiPaperTransport(
            intent_loader=lambda _intent_id: frozen,
            order_factory=factory,
            submitter=SimpleNamespace(submit_precommitted=lambda **kwargs: calls.append(kwargs)),
        )
        command = SimpleNamespace(
            instrument=frozen.instrument_id, side="BUY", quantity=3,
            order_type="market", time_in_force="DAY",
        )

        transport(
            command, intent_id="intent-1", broker_order_id=7,
            attempt_id="attempt-1", client_order_id="client-1",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["intent_id"], "intent-1")
        self.assertEqual(calls[0]["broker_order_id"], 7)
        self.assertIsInstance(calls[0]["contract"], Contract)
        self.assertIsInstance(calls[0]["order"], Order)


if __name__ == "__main__":
    unittest.main()
