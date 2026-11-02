from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.execution.ibkr_completed_bar_source import (
    IbkrCompletedBarSourceError,
    IbkrPaperCompletedBarSource,
)
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.ibkr_paper_risk_facts import VerifiedIbkrContract
from src.trading.connectors.ibkr.local import IBKRLocalConfig


ACCOUNT = "DU1234567"
CONTRACT = VerifiedIbkrContract("AAPL", "AAPL", 265598)
_DATES = ("2026-10-28", "2026-10-29", "2026-10-30")


def _config(**changes):
    return IBKRLocalConfig.from_mapping({
        "profile": "paper", "host": "127.0.0.1", "port": 4002,
        "client_id": 77, "account": ACCOUNT, "readonly": True,
        "market_data_type": 3, **changes,
    })


def _response(dates=_DATES, *, contract=None):
    return {
        "status": "ok",
        "qualified_contract": contract or {
            "symbol": "AAPL", "con_id": 265598, "sec_type": "STK",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD",
        },
        "bars": [
            {"date": day, "open": "100", "high": "102", "low": "99", "close": "101", "volume": "1234"}
            for day in dates
        ],
    }


def _source(now, **config_overrides):
    return IbkrPaperCompletedBarSource(
        config=_config(**config_overrides), account_ref=ACCOUNT, contract=CONTRACT,
        now=lambda: now,
    )


def test_source_returns_exact_contract_completed_xnys_daily_bars(monkeypatch):
    from src.trading.connectors.ibkr import local

    calls = []
    monkeypatch.setattr(local, "get_historical_bars", lambda symbol, **kwargs: calls.append((symbol, kwargs)) or _response())
    now = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    bars = _source(now)(after_session=date(2026, 10, 28))

    assert [bar.session_date for bar in bars] == [date(2026, 10, 29), date(2026, 10, 30)]
    assert all(bar.is_final and bar.instrument_id == "AAPL" for bar in bars)
    assert all(bar.source == "ibkr_paper_gateway_historical_trades" for bar in bars)
    assert all("corporate_action_adjustment_unverified" in bar.quality_flags for bar in bars)
    assert bars[-1].close == Decimal("101")
    assert all(bar.available_at == bar.end_at for bar in bars)
    symbol, kwargs = calls[0]
    assert symbol == "AAPL"
    assert kwargs["use_rth"] is True and kwargs["what_to_show"] == "TRADES"
    assert kwargs["bar_size"] == "1 day"


def test_source_drops_current_incomplete_session(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response())
    # 14:00 ET on 2026-10-30, before the 16:00 regular close.
    now = datetime(2026, 10, 30, 18, tzinfo=timezone.utc)
    bars = _source(now)()
    assert [bar.session_date for bar in bars] == [date(2026, 10, 28), date(2026, 10, 29)]


def test_completed_bar_requires_exact_instrument_and_completed_session(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response())
    source = _source(datetime(2026, 10, 30, 18, tzinfo=timezone.utc))
    assert source.completed_bar("AAPL", date(2026, 10, 29)).close == Decimal("101")
    with pytest.raises(IbkrCompletedBarSourceError, match="unavailable"):
        source.completed_bar("AAPL", date(2026, 10, 30))
    with pytest.raises(IbkrCompletedBarSourceError, match="differs"):
        source.completed_bar("MSFT", date(2026, 10, 29))


def test_source_publishes_only_to_the_matching_bounded_paper_feed(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response())
    now = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    feed = PaperCompletedBarFeed(
        instrument_id="AAPL", timezone="America/New_York", currency="USD",
        queue_size=4, now=lambda: now,
    )

    assert _source(now).publish(feed, after_session=date(2026, 10, 28)) == 2
    assert feed.pending_bar_count == 2
    assert feed.failure is None

    wrong_feed = PaperCompletedBarFeed(
        instrument_id="MSFT", timezone="America/New_York", currency="USD", queue_size=4, now=lambda: now,
    )
    with pytest.raises(IbkrCompletedBarSourceError, match="feed differs"):
        _source(now).publish(wrong_feed)


def test_incremental_source_rejects_missing_completed_xnys_session(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response(("2026-10-28", "2026-10-30")))
    with pytest.raises(IbkrCompletedBarSourceError, match="missing one or more"):
        _source(datetime(2026, 10, 31, 15, tzinfo=timezone.utc))(after_session=date(2026, 10, 28))


@pytest.mark.parametrize("dates", [("2026-10-28", "2026-10-29"), ("2026-10-28",)])
def test_incremental_source_rejects_missing_latest_or_all_new_sessions(monkeypatch, dates):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response(dates))
    with pytest.raises(IbkrCompletedBarSourceError, match="missing one or more"):
        _source(datetime(2026, 10, 31, 15, tzinfo=timezone.utc))(after_session=date(2026, 10, 28))


def test_incremental_source_accepts_no_new_closed_session(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response())
    assert _source(datetime(2026, 10, 30, 18, tzinfo=timezone.utc))(after_session=date(2026, 10, 29)) == ()


def test_incremental_source_rejects_future_cursor(monkeypatch):
    from src.trading.connectors.ibkr import local

    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response())
    with pytest.raises(IbkrCompletedBarSourceError, match="future"):
        _source(datetime(2026, 10, 30, 18, tzinfo=timezone.utc))(after_session=date(2026, 10, 31))


def test_source_rejects_contract_mismatch(monkeypatch):
    from src.trading.connectors.ibkr import local

    wrong = {**_response()["qualified_contract"], "con_id": 1}
    monkeypatch.setattr(local, "get_historical_bars", lambda *_args, **_kwargs: _response(contract=wrong))
    with pytest.raises(IbkrCompletedBarSourceError, match="contract differs"):
        _source(datetime(2026, 10, 31, 15, tzinfo=timezone.utc))()


@pytest.mark.parametrize("overrides", [
    {"profile": "live-readonly", "port": 4001},
    {"readonly": False},
    {"account": "DU9999999"},
])
def test_source_rejects_wrong_or_mutable_gateway_profile(overrides):
    with pytest.raises(ValueError, match="PAPER read-only"):
        _source(datetime(2026, 10, 31, 15, tzinfo=timezone.utc), **overrides)
