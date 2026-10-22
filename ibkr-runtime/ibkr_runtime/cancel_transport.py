"""Exact-ID cancellation boundary for a pre-authorized owned IBKR order."""

from __future__ import annotations

from typing import Any


class IbapiCancelTransport:
    """Call the official SDK cancel method for one positive broker order ID.

    Ownership, account scope, active-state checks, and the durable
    CANCEL_PENDING transition belong to the trusted host before this boundary.
    This adapter never discovers or cancels orders by account or symbol.
    """

    def __init__(self, *, client: Any) -> None:
        if client is None or not callable(getattr(client, "cancelOrder", None)):
            raise TypeError("an official IB API client with cancelOrder is required")
        self.client = client

    def __call__(self, broker_order_id: int) -> None:
        if type(broker_order_id) is not int or broker_order_id <= 0:
            raise ValueError("broker_order_id must be a positive integer")
        # TWS API 10.50 requires the SDK's typed OrderCancel object.
        from ibapi.order_cancel import OrderCancel

        self.client.cancelOrder(broker_order_id, OrderCancel())
