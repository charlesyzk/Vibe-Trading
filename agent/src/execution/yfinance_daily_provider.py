"""Exact-source adapter for the existing YFinance daily loader.

This adapter records what the host observed while calling the Vibe YFinance
loader. It does not authenticate Yahoo/YFinance, its adjustments, historical
publication times, or a symbol's broker contract mapping.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd


class YFinanceProviderError(ValueError):
    """A requested exact YFinance fetch could not be completed as requested."""


@dataclass(frozen=True, slots=True)
class YFinanceDailyResult:
    symbol: str
    frame: pd.DataFrame
    provenance: dict[str, Any]


def canonical_loader_frame_sha256(frame: pd.DataFrame) -> str:
    """Hash the exact tabular values returned by the loader in stable JSON form."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise YFinanceProviderError("YFinance loader returned no tabular bars")
    columns = [list(column) if isinstance(column, tuple) else str(column) for column in frame.columns]
    rows: list[dict[str, Any]] = []
    for index_value, values in zip(frame.index, frame.itertuples(index=False, name=None), strict=True):
        timestamp = pd.Timestamp(index_value)
        if timestamp.tzinfo is None:
            index_text = timestamp.isoformat()
        else:
            index_text = timestamp.tz_convert("UTC").isoformat()
        rows.append({
            "index": index_text,
            "values": [
                format(float(value), ".17g") if isinstance(value, (int, float)) else str(value)
                for value in values
            ],
        })
    payload = json.dumps(
        {"columns": columns, "index_names": [str(value) for value in frame.index.names], "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class YFinanceDailyProvider:
    """Call the concrete YFinance loader directly, without registry fallback.

    ``loader_factory`` is an injection seam for deterministic tests. Production
    uses the concrete Vibe loader class; it never asks the source registry to
    select a replacement provider.
    """

    def __init__(
        self,
        *,
        loader_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        cache_probe: Callable[[str, date, date], bool] | None = None,
    ) -> None:
        self._loader_factory = loader_factory or self._default_loader_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache_probe = cache_probe or self._default_cache_probe

    @staticmethod
    def _default_loader_factory() -> Any:
        from backtest.loaders.yfinance_loader import DataLoader

        return DataLoader()

    @staticmethod
    def _default_cache_probe(symbol: str, start: date, end: date) -> bool:
        from backtest.loaders.base import loader_cache_get
        from backtest.loaders.yfinance_loader import _to_yfinance_symbol

        cached = loader_cache_get(
            source="yfinance",
            symbol=_to_yfinance_symbol(symbol),
            timeframe="1D",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            fields=None,
        )
        return cached is not None

    def history(self, symbol: str, start: date, end: date) -> YFinanceDailyResult:
        if not symbol or not isinstance(symbol, str):
            raise YFinanceProviderError("symbol must be a non-empty string")
        if not isinstance(start, date) or not isinstance(end, date) or end < start:
            raise YFinanceProviderError("YFinance date range is invalid")

        loader = self._loader_factory()
        if getattr(loader, "name", None) != "yfinance":
            raise YFinanceProviderError("exact YFinance adapter received a loader with a different identity")
        if not callable(getattr(loader, "fetch", None)):
            raise YFinanceProviderError("YFinance loader does not expose fetch()")

        cache_hit = self._cache_probe(symbol, start, end)
        try:
            response = loader.fetch([symbol], start.isoformat(), end.isoformat(), interval="1D", fields=None)
        except Exception as exc:
            raise YFinanceProviderError("exact YFinance history request failed") from exc
        if not isinstance(response, dict) or set(response) != {symbol}:
            raise YFinanceProviderError("exact YFinance request did not return the requested symbol")
        frame = response[symbol]
        digest = canonical_loader_frame_sha256(frame)
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise YFinanceProviderError("host retrieval clock must return a timezone-aware timestamp")
        retrieved_at = retrieved_at.astimezone(timezone.utc)

        return YFinanceDailyResult(
            symbol=symbol,
            frame=frame.copy(),
            provenance={
                "schema": "host-observed-provider-fetch/1",
                "requested_provider": "yfinance",
                "actual_loader_identity": "yfinance",
                "loader_class": f"{type(loader).__module__}.{type(loader).__qualname__}",
                "request_parameters": {
                    "symbol": symbol,
                    "start_date": start.isoformat(),
                    "end_date_inclusive": end.isoformat(),
                    "interval": "1D",
                    "fields": None,
                },
                "retrieved_at": retrieved_at.isoformat(),
                "loader_cache": "hit" if cache_hit else "miss",
                # The legacy loader normalizes provider responses before this
                # boundary; do not label its output as raw HTTP payload bytes.
                "loader_output_sha256": digest,
                "identity_authentication": "UNVERIFIED",
                "point_in_time_availability": "UNVERIFIED",
                "adjustment_semantics": "UNVERIFIED",
                "broker_contract_mapping": "UNVERIFIED",
            },
        )
