"""Minimal PAPER order/cancel SDK calls exposed to the trusted app parent."""

from __future__ import annotations

from typing import Any

from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

from .ipc import PROTOCOL_VERSION, validate_request
from .lifecycle import ClientLifecycle, IbapiClientOwner


def execute(owner: IbapiClientOwner, command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and issue one constrained SDK operation; never do account discovery."""
    validate_request({
        "protocol": PROTOCOL_VERSION,
        "type": "command",
        "request_id": "trusted-runtime-command-id",
        "command": command,
        "payload": payload,
    })
    if owner.state is not ClientLifecycle.RUNNING:
        raise RuntimeError("IBKR SDK client is not running")

    if command == "request_order_ids":
        owner.request_order_ids()
        return {"requested": True}

    if command == "request_reconciliation":
        return {"snapshot_id": owner.request_reconciliation_snapshot(payload["snapshot_id"])}
    broker_order_id = payload["broker_order_id"]
    client = owner.client
    if command == "cancel_order":
        client.cancelOrder(broker_order_id, OrderCancel())
        return {"broker_order_id": broker_order_id, "operation": "cancel_requested"}
    if command != "place_order":
        raise ValueError("unsupported SDK operation")

    contract_data = payload["contract"]
    contract = Contract()
    contract.conId = contract_data["con_id"]
    contract.symbol = contract_data["symbol"]
    contract.secType = contract_data["security_type"]
    contract.exchange = contract_data["exchange"]
    contract.primaryExchange = contract_data["primary_exchange"]
    contract.currency = contract_data["currency"]

    order_data = payload["order"]
    order = Order()
    order.action = order_data["action"]
    order.totalQuantity = order_data["quantity"]
    order.orderType = order_data["order_type"]
    order.tif = order_data["time_in_force"]
    order.account = order_data["account_ref"]
    order.orderRef = order_data["client_order_id"]
    order.outsideRth = order_data["outside_rth"]
    order.transmit = order_data["transmit"]
    client.placeOrder(broker_order_id, contract, order)
    return {"broker_order_id": broker_order_id, "operation": "place_requested"}
