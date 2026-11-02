from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from src.execution.ibkr_paper_risk_facts import IbkrPaperRiskFactsError, StrategyRiskMetrics
from src.execution.order_ledger import BrokerEventKind, SQLiteOrderLedger
from src.execution.paper_strategy_metrics import LedgerComputedStrategyMetricsReader, LedgerStrategyMetricsReader
from tests.execution.test_order_ledger import VERSION, intent, report


def test_counts_scoped_attempts_in_new_york_session_and_keeps_host_metrics(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "paper.sqlite")
    for index, account in enumerate(("DU123", "DU123", "DU999")):
        order = ledger.create_intent(intent(broker="IBKR", account_ref=account,
                                            decision_id=f"decision-{index}"))
        ledger.reserve(order.intent_id, f"reservation-{index}", "10")
        ledger.start_send(order.intent_id, attempt_id=f"attempt-{index}", client_order_id=f"client-{index}")
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE submission_attempt SET started_at = ? WHERE attempt_id = ?",
                   ("2026-11-02T15:00:00+00:00", "attempt-0"))
        db.execute("UPDATE submission_attempt SET started_at = ? WHERE attempt_id = ?",
                   ("2026-11-02T04:30:00+00:00", "attempt-1"))  # Previous New York day.
        db.execute("UPDATE submission_attempt SET started_at = ? WHERE attempt_id = ?",
                   ("2026-11-02T15:00:00+00:00", "attempt-2"))
    facts = StrategyRiskMetrics(0, 0, Decimal("0"), Decimal("12"), 0, Decimal("0.05"), Decimal("1"))
    reader = LedgerStrategyMetricsReader(database=ledger.path, account_ref="DU123", source=lambda *_: facts)
    now = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
    result = reader(VERSION, "fixture-equity-1", now)
    assert result.orders_this_session == 1
    assert result.strategy_daily_loss == Decimal("12")
    assert reader("b" * 64, "fixture-equity-1", now).orders_this_session == 0
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE submission_attempt SET started_at = ? WHERE attempt_id = ?",
                   ("2026-11-02T17:00:00+00:00", "attempt-0"))
    with pytest.raises(IbkrPaperRiskFactsError, match="count is unavailable"):
        reader(VERSION, "fixture-equity-1", now)


def test_strategy_position_is_derived_from_scoped_fills_before_snapshot(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "paper.sqlite")
    for index, account in enumerate(("DU123", "DU999")):
        order = ledger.create_intent(intent(broker="IBKR", account_ref=account, decision_id=f"position-{index}"))
        ledger.reserve(order.intent_id, f"reservation-{index}", "150")
        ledger.start_send(order.intent_id, attempt_id=f"attempt-{index}", client_order_id=f"client-{index}")
        ledger.record_broker_event(report(order.intent_id, BrokerEventKind.FILL, f"fill-{index}",
                                          broker="IBKR", account_ref=account, broker_exec_id=f"exec-{index}",
                                          quantity=3, price=Decimal("10")))
    reader = LedgerStrategyMetricsReader(database=ledger.path, account_ref="DU123", source=lambda *_: None)
    assert reader.position_at(VERSION, "fixture-equity-1", datetime(2026, 10, 20, 14, 1, tzinfo=timezone.utc)) == 3
    assert reader.position_at("b" * 64, "fixture-equity-1", datetime(2026, 10, 20, 14, 1, tzinfo=timezone.utc)) == 0
    with pytest.raises(IbkrPaperRiskFactsError, match="position cannot be established"):
        reader.position_at(VERSION, "fixture-equity-1", datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc))


def test_daily_loss_uses_fills_fee_and_previous_session_close(tmp_path):
    from types import SimpleNamespace

    ledger = SQLiteOrderLedger(tmp_path / "paper.sqlite")
    order = ledger.create_intent(intent(broker="IBKR", account_ref="DU123"))
    ledger.reserve(order.intent_id, "r", "150")
    ledger.start_send(order.intent_id, attempt_id="a", client_order_id="c")
    ledger.record_broker_event(report(order.intent_id, BrokerEventKind.FILL, "event", broker="IBKR",
                                      account_ref="DU123", broker_exec_id="exec", quantity=3, price=Decimal("10")))
    reader = LedgerStrategyMetricsReader(database=ledger.path, account_ref="DU123", source=lambda *_: None,
        previous_bar_reader=lambda instrument, day: SimpleNamespace(
            instrument_id=instrument, session_date=day, is_final=True, close=Decimal("12")))
    as_of = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(IbkrPaperRiskFactsError, match="daily loss facts are unavailable"):
        reader.daily_loss_at(VERSION, "fixture-equity-1", as_of, Decimal("8"))
    ledger.append_fee_fact(broker="IBKR", account_ref="DU123", environment="paper", broker_exec_id="exec",
                           amount="1", currency="USD", received_at=as_of, evidence={"fee": "1"})
    assert reader.daily_loss_at(VERSION, "fixture-equity-1", as_of, Decimal("8")) == Decimal("7")
    next_day = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
    assert reader.daily_loss_at(VERSION, "fixture-equity-1", next_day, Decimal("10")) == Decimal("6")


def test_computed_metrics_require_snapshot_and_explicit_valid_costs(tmp_path):
    ledger = SQLiteOrderLedger(tmp_path / "paper.sqlite")
    with pytest.raises(ValueError, match="previous-session"):
        LedgerComputedStrategyMetricsReader(database=ledger.path, account_ref="DU123", previous_bar_reader=None,
                                            price_buffer_fraction=Decimal("0.05"), estimated_fees=Decimal("1"))
    with pytest.raises(ValueError, match="price buffer"):
        LedgerComputedStrategyMetricsReader(database=ledger.path, account_ref="DU123", previous_bar_reader=lambda *_: None,
                                            price_buffer_fraction=Decimal("-0.1"), estimated_fees=Decimal("1"))
    reader = LedgerComputedStrategyMetricsReader(database=ledger.path, account_ref="DU123", previous_bar_reader=lambda *_: None,
                                                  price_buffer_fraction=Decimal("0.05"), estimated_fees=Decimal("1"))
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(IbkrPaperRiskFactsError, match="require a reconciled snapshot"):
        reader(VERSION, "fixture-equity-1", now)
    facts = reader.for_snapshot(VERSION, "fixture-equity-1", now, now, Decimal("10"))
    assert facts.strategy_position_qty == 0 and facts.strategy_daily_loss == 0
    assert facts.estimated_fees == Decimal("1") and facts.price_buffer_fraction == Decimal("0.05")
