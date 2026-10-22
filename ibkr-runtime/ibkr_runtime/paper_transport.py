"""Compose verified SDK objects and a precommitted host send boundary."""

from __future__ import annotations

from typing import Any, Callable


class IbapiPaperTransport:
    """Callable transport accepted by ``BacktraderDispatchPort``.

    The app host injects its ledger-backed intent lookup and the SDK-only
    order factory/submitter. This object never connects or performs discovery.
    """

    def __init__(
        self,
        *,
        intent_loader: Callable[[str], Any],
        order_factory: Any,
        submitter: Any,
    ) -> None:
        self.intent_loader = intent_loader
        self.order_factory = order_factory
        self.submitter = submitter

    def __call__(
        self,
        command: Any,
        *,
        intent_id: str,
        broker_order_id: int,
        attempt_id: str,
        client_order_id: str,
    ) -> None:
        intent = self.intent_loader(intent_id)
        expected = (
            intent.instrument_id == command.instrument
            and intent.side == command.side
            and intent.integer_qty == command.quantity
            and intent.order_type == command.order_type
            and intent.time_in_force == command.time_in_force
        )
        if not expected:
            raise ValueError("native Backtrader command differs from the frozen intent")
        contract, order = self.order_factory.build(intent)
        self.submitter.submit_precommitted(
            intent_id=intent_id,
            broker_order_id=broker_order_id,
            contract=contract,
            order=order,
        )
