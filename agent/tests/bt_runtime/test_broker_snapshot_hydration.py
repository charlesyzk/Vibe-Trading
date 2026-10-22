from __future__ import annotations

import datetime as dt

import backtrader as bt
import pytest

from src.bt_runtime.brokers.proxy import BrokerCallback, CallbackStatus, PortBroker


class _Port:
    def register_callback_sink(self, sink):
        self.sink = sink

    def submit_order(self, _command):
        raise AssertionError("hydration test must not submit orders")

    def cancel_order(self, _command):
        raise AssertionError("hydration test must not cancel orders")


class _Feed(bt.feed.DataBase):
    def _load(self):
        return False


def _snapshot(**overrides):
    value = {
        "snapshot_id": 43,
        "account_ref": "DU123456",
        "environment": "paper",
        "capture_complete": True,
        "reconciled": True,
        "requested_at_utc": "2026-01-01T00:00:00+00:00",
        "completed_at_utc": "2026-01-01T00:00:01+00:00",
        "open_orders": [],
        "reconciliation_result": {
            "snapshot_id": 43,
            "account_ref": "DU123456",
            "environment": "paper",
            "checks": ["scope", "open_orders", "positions", "order_id_high_water_mark"],
        },
        "account_summary": [
            {"snapshot_id": 43, "account": "DU123456", "tag": "NetLiquidation", "currency": "USD", "value": "12000"},
            {"snapshot_id": 43, "account": "DU123456", "tag": "TotalCashValue", "currency": "USD", "value": "8500.25"},
            {"snapshot_id": 43, "account": "DU123456", "tag": "AvailableFunds", "currency": "USD", "value": "8000"},
        ],
        "positions": [
            {"snapshot_id": 43, "account": "DU123456", "contract_id": 111, "symbol": "ABC", "currency": "USD", "quantity": "7", "average_cost": "50.25"},
        ],
    }
    value.update(overrides)
    return value


def _hydrate(broker, snapshot=None, *, mapping=None, account_ref="DU123456", environment="paper"):
    return broker.hydrate_reconciled_account_snapshot(
        _snapshot() if snapshot is None else snapshot,
        account_ref=account_ref,
        environment=environment,
        data_by_con_id={111: _Feed()} if mapping is None else mapping,
    )


def test_hydrates_cash_and_positions_from_exact_reconciled_paper_snapshot():
    broker = PortBroker(_Port(), initial_cash=100_000)
    feed = _Feed()
    broker.hydrate_reconciled_account_snapshot(
        _snapshot(), account_ref="DU123456", environment="paper", data_by_con_id={111: feed}
    )

    assert broker.getcash() == pytest.approx(8500.25)
    assert broker.startingcash == pytest.approx(8500.25)
    position = broker.getposition(feed)
    assert position.size == 7
    assert position.price == pytest.approx(50.25)
    with pytest.raises(RuntimeError, match="market value is unavailable"):
        broker.getvalue()


@pytest.mark.parametrize(
    ("snapshot", "account_ref", "environment", "message"),
    [
        (_snapshot(capture_complete=False), "DU123456", "paper", "complete, reconciled"),
        (_snapshot(reconciled=False), "DU123456", "paper", "complete, reconciled"),
        (_snapshot(environment="live"), "DU123456", "paper", "complete, reconciled"),
        (_snapshot(), "DU-other", "paper", "complete, reconciled"),
        (_snapshot(), "DU123456", "live", "PAPER only"),
        (_snapshot(reconciliation_result=None), "DU123456", "paper", "durable reconciliation proof"),
    ],
)
def test_rejects_incomplete_wrong_scope_or_unproven_snapshot(snapshot, account_ref, environment, message):
    broker = PortBroker(_Port(), initial_cash=321)
    with pytest.raises(ValueError, match=message):
        _hydrate(broker, snapshot, account_ref=account_ref, environment=environment)
    assert broker.getcash() == 321


def test_requires_exactly_one_usd_total_cash_value_and_matching_rows():
    broker = PortBroker(_Port(), initial_cash=321)
    snap = _snapshot(account_summary=[
        {"snapshot_id": 43, "account": "DU123456", "tag": "NetLiquidation", "currency": "USD", "value": "12000"},
        {"snapshot_id": 43, "account": "DU123456", "tag": "TotalCashValue", "currency": "USD", "value": "8500"},
        {"snapshot_id": 43, "account": "DU123456", "tag": "TotalCashValue", "currency": "USD", "value": "8501"},
    ])
    with pytest.raises(ValueError, match="repeats an account summary tag"):
        _hydrate(broker, snap)
    assert broker.getcash() == 321

    bad_row = _snapshot(account_summary=[
        {"snapshot_id": 43, "account": "OTHER", "tag": "TotalCashValue", "currency": "USD", "value": "8500"},
    ])
    with pytest.raises(ValueError, match="row scope"):
        _hydrate(broker, bad_row)


def test_requires_full_unambiguous_conid_mapping_and_valid_position_rows():
    broker = PortBroker(_Port(), initial_cash=321)
    feed = _Feed()
    with pytest.raises(ValueError, match="no Backtrader feed mapping"):
        _hydrate(broker, mapping={})
    assert broker.getcash() == 321
    assert broker.getposition(feed).size == 0
    shared_feed = _Feed()
    with pytest.raises(ValueError, match="multiple contract ids"):
        _hydrate(broker, mapping={111: shared_feed, 222: shared_feed})
    duplicate = _snapshot(positions=[_snapshot()["positions"][0], _snapshot()["positions"][0]])
    with pytest.raises(ValueError, match="repeats a contract position"):
        _hydrate(broker, duplicate)
    fractional = _snapshot(positions=[{**_snapshot()["positions"][0], "quantity": "1.5"}])
    with pytest.raises(ValueError, match="whole shares"):
        _hydrate(broker, fractional)
    assert broker.getcash() == 321
    assert broker.getposition(feed).size == 0


def test_hydration_is_prestart_one_shot_and_rejects_any_order_or_callback_activity():
    broker = PortBroker(_Port(), initial_cash=321)
    _hydrate(broker)
    with pytest.raises(RuntimeError, match="already hydrated"):
        _hydrate(broker)

    started = PortBroker(_Port(), initial_cash=321)
    started.start()
    with pytest.raises(RuntimeError, match="before broker start"):
        _hydrate(started)

    ordered = PortBroker(_Port(), initial_cash=321)
    feed = _Feed()
    order = bt.BuyOrder(owner=None, data=feed, size=1, simulated=True, price=10)
    ordered._register_order(order)
    with pytest.raises(RuntimeError, match="no prior orders or broker callbacks"):
        _hydrate(ordered)

    callbacked = PortBroker(_Port(), initial_cash=321)
    callbacked.on_callback(BrokerCallback(
        order_ref=1,
        status=CallbackStatus.ACCEPTED,
        event_id="unmatched-callback",
        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    ))
    with pytest.raises(RuntimeError, match="no prior orders or broker callbacks"):
        _hydrate(callbacked)
