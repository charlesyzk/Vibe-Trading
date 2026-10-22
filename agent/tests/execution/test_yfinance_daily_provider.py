from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src.execution.yfinance_daily_provider import (
    YFinanceDailyProvider,
    YFinanceProviderError,
    canonical_loader_frame_sha256,
)


class _FakeLoader:
    name = "yfinance"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def fetch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def _frame():
    return pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [100.0, 120.0],
        },
        index=pd.DatetimeIndex(["2025-02-03", "2025-02-04"], name="trade_date"),
    )


@pytest.mark.parametrize(("cache_hit", "expected"), [(False, "miss"), (True, "hit")])
def test_exact_yfinance_adapter_records_host_observed_request_and_response(cache_hit, expected):
    frame = _frame()
    loader = _FakeLoader({"AAPL.US": frame})
    retrieved_at = datetime(2025, 2, 5, 12, tzinfo=timezone.utc)
    provider = YFinanceDailyProvider(
        loader_factory=lambda: loader,
        clock=lambda: retrieved_at,
        cache_probe=lambda symbol, start, end: cache_hit,
    )

    result = provider.history("AAPL.US", date(2025, 2, 3), date(2025, 2, 4))

    assert loader.calls == [
        ((["AAPL.US"], "2025-02-03", "2025-02-04"), {"interval": "1D", "fields": None}),
    ]
    assert result.provenance["requested_provider"] == "yfinance"
    assert result.provenance["actual_loader_identity"] == "yfinance"
    assert result.provenance["retrieved_at"] == retrieved_at.isoformat()
    assert result.provenance["loader_cache"] == expected
    assert result.provenance["loader_output_sha256"] == canonical_loader_frame_sha256(frame)
    assert result.provenance["identity_authentication"] == "UNVERIFIED"
    assert result.provenance["point_in_time_availability"] == "UNVERIFIED"
    assert result.provenance["adjustment_semantics"] == "UNVERIFIED"
    assert result.provenance["broker_contract_mapping"] == "UNVERIFIED"
    pd.testing.assert_frame_equal(result.frame, frame)


def test_exact_yfinance_adapter_rejects_registry_substitute_identity():
    substitute = _FakeLoader({"AAPL.US": _frame()})
    substitute.name = "stooq"
    provider = YFinanceDailyProvider(loader_factory=lambda: substitute)

    with pytest.raises(YFinanceProviderError, match="different identity"):
        provider.history("AAPL.US", date(2025, 2, 3), date(2025, 2, 4))

    assert substitute.calls == []


def test_exact_yfinance_adapter_does_not_fill_missing_symbol_from_another_provider():
    loader = _FakeLoader({})
    provider = YFinanceDailyProvider(
        loader_factory=lambda: loader,
        cache_probe=lambda *_: False,
    )

    with pytest.raises(YFinanceProviderError, match="did not return the requested symbol"):
        provider.history("AAPL.US", date(2025, 2, 3), date(2025, 2, 4))


def test_exact_yfinance_adapter_rejects_naive_host_retrieval_time():
    provider = YFinanceDailyProvider(
        loader_factory=lambda: _FakeLoader({"AAPL.US": _frame()}),
        clock=lambda: datetime(2025, 2, 5),
        cache_probe=lambda *_: True,
    )

    with pytest.raises(YFinanceProviderError, match="timezone-aware"):
        provider.history("AAPL.US", date(2025, 2, 3), date(2025, 2, 4))
