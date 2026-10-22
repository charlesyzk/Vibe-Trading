from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.execution.ibkr_daily_provider import IbkrDailyProvider, IbkrDailyProviderError


def _response(*, profile_id="ibkr-live-local-readonly", environment="live", readonly=True):
    return {
        "status": "ok",
        "profile_id": profile_id,
        "connector": "ibkr",
        "environment": environment,
        "transport": "local_tws",
        "readonly": readonly,
        "qualified_contract": {
            "symbol": "AAPL", "sec_type": "STK", "currency": "USD", "con_id": 265598,
            "exchange": "SMART", "primary_exchange": "NASDAQ", "local_symbol": "AAPL",
        },
        "bars": [
            {"date": "20261020", "open": 250, "high": 252, "low": 249, "close": 251, "volume": 1000},
            {"date": "20261021", "open": 251, "high": 253, "low": 250, "close": 252, "volume": 1100},
            {"date": "20261022", "open": 252, "high": 254, "low": 251, "close": 253, "volume": 1200},
        ],
    }


def test_fetches_exact_symbol_and_range_from_explicit_live_readonly_profile():
    calls = []

    def fetch(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return _response()

    provider = IbkrDailyProvider(
        history_fn=fetch,
        clock=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc),
    )
    result = provider.history(
        "aapl", date(2026, 10, 20), date(2026, 10, 22),
        instrument_id="AAPL.US", profile_id="ibkr-live-local-readonly",
        port=4001,
    )

    assert calls[0][0] == "AAPL"
    assert calls[0][1] == {
        "profile_id": "ibkr-live-local-readonly",
        "exchange": "SMART", "currency": "USD", "sec_type": "STK",
        "duration": "23 D", "end_date": date(2026, 10, 22), "bar_size": "1 day",
        "what_to_show": "TRADES", "use_rth": True, "readonly": True,
        "port": 4001,
    }
    assert [stamp.date().isoformat() for stamp in result.frame.index] == [
        "2026-10-20", "2026-10-21", "2026-10-22",
    ]
    assert result.provenance["environment"] == "live"
    assert result.provenance["qualified_contract"]["con_id"] == 265598
    assert result.provenance["request_parameters"]["what_to_show"] == "TRADES"


@pytest.mark.parametrize(
    "changes",
    [
        {"readonly": False},
        {"transport": "remote_mcp"},
        {"connector": "other"},
        {"profile_id": "ibkr-live-official-mcp-readonly"},
    ],
)
def test_refuses_non_local_or_writable_profile(changes):
    response = _response()
    response.update(changes)
    provider = IbkrDailyProvider(history_fn=lambda *_args, **_kwargs: response)

    with pytest.raises(IbkrDailyProviderError, match="read-only connector profile"):
        provider.history(
            "AAPL", date(2026, 10, 20), date(2026, 10, 22),
            instrument_id="AAPL.US", profile_id="ibkr-live-local-readonly",
        )


def test_refuses_unqualified_or_mismatched_contract():
    provider = IbkrDailyProvider(history_fn=lambda *_args, **_kwargs: {
        **_response(),
        "qualified_contract": {"symbol": "AAPL", "sec_type": "STK", "currency": "USD", "con_id": 0},
    })
    with pytest.raises(IbkrDailyProviderError, match="positive conId"):
        provider.history(
            "AAPL", date(2026, 10, 20), date(2026, 10, 22),
            instrument_id="AAPL.US", profile_id="ibkr-live-local-readonly",
        )


def test_refuses_dates_outside_requested_range_and_duplicate_sessions():
    outside = _response()
    outside["bars"] = [outside["bars"][0]]
    provider = IbkrDailyProvider(history_fn=lambda *_args, **_kwargs: outside)
    with pytest.raises(IbkrDailyProviderError, match="no bars inside"):
        provider.history(
            "AAPL", date(2026, 10, 21), date(2026, 10, 22),
            instrument_id="AAPL.US", profile_id="ibkr-live-local-readonly",
        )

    duplicate = _response()
    duplicate["bars"] = [duplicate["bars"][1], duplicate["bars"][1]]
    provider = IbkrDailyProvider(history_fn=lambda *_args, **_kwargs: duplicate)
    with pytest.raises(IbkrDailyProviderError, match="unique increasing"):
        provider.history(
            "AAPL", date(2026, 10, 20), date(2026, 10, 22),
            instrument_id="AAPL.US", profile_id="ibkr-live-local-readonly",
        )
