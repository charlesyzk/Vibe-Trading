"""Read-only daily history adapter for the local IBKR TWS/Gateway session.

This adapter uses the user's explicitly selected local IBKR read-only profile.
It fetches bars during preview; the caller freezes the returned rows before
the Backtrader run so confirmation never issues another broker request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import ceil
from typing import Any, Callable

import pandas as pd


class IbkrDailyProviderError(ValueError):
    """The requested IBKR daily history could not be verified for import."""


@dataclass(frozen=True, slots=True)
class IbkrDailyResult:
    symbol: str
    frame: pd.DataFrame
    provenance: dict[str, Any]


class IbkrDailyProvider:
    """Fetch regular-session USD stock TRADES bars from local read-only IBKR."""

    def __init__(
        self,
        *,
        history_fn: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._history_fn = history_fn or self._default_history_fn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _default_history_fn(symbol: str, *, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        from src.trading.service import get_history
        from src.trading.profiles import profile_by_id

        profile = profile_by_id(profile_id)
        if (
            profile.id not in {"ibkr-paper-local", "ibkr-live-local-readonly"}
            or profile.connector != "ibkr"
            or profile.transport != "local_tws"
            or not profile.readonly
        ):
            raise IbkrDailyProviderError("select an IBKR local read-only profile before fetching history")
        return get_history(symbol, profile.id, **kwargs)

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        instrument_id: str,
        profile_id: str,
        port: int | None = None,
    ) -> IbkrDailyResult:
        clean_symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not clean_symbol or start > end:
            raise IbkrDailyProviderError("IBKR history symbol or date range is invalid")
        if not instrument_id.strip():
            raise IbkrDailyProviderError("instrument_id must be non-empty")
        if profile_id not in {"ibkr-paper-local", "ibkr-live-local-readonly"}:
            raise IbkrDailyProviderError("IBKR history profile must be local PAPER or live read-only")
        valid_ports = {4002, 7497} if profile_id == "ibkr-paper-local" else {4001, 7496}
        if port is not None and port not in valid_ports:
            raise IbkrDailyProviderError("IBKR socket port does not match the selected environment")

        requested_days = (end - start).days + 1
        padded_days = requested_days + 20
        duration = f"{padded_days} D" if padded_days <= 365 else f"{ceil(padded_days / 365)} Y"
        try:
            response = self._history_fn(
                clean_symbol,
                profile_id=profile_id,
                exchange="SMART",
                currency="USD",
                sec_type="STK",
                duration=duration,
                end_date=end,
                bar_size="1 day",
                what_to_show="TRADES",
                use_rth=True,
                readonly=True,
                **({"port": port} if port is not None else {}),
            )
        except Exception as exc:
            raise IbkrDailyProviderError("IBKR local read-only historical-data request failed") from exc
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise IbkrDailyProviderError("IBKR historical-data request returned no successful result")
        if (
            response.get("connector") != "ibkr"
            or response.get("transport") != "local_tws"
            or response.get("profile_id") != profile_id
            or response.get("environment") != ("paper" if profile_id == "ibkr-paper-local" else "live")
            or response.get("readonly") is not True
        ):
            raise IbkrDailyProviderError("IBKR history requires an explicit local read-only connector profile")

        contract = response.get("qualified_contract")
        if not isinstance(contract, dict):
            raise IbkrDailyProviderError("IBKR did not return qualified contract identity")
        expected_contract = {
            "symbol": clean_symbol,
            "sec_type": "STK",
            "currency": "USD",
        }
        if any(str(contract.get(key, "")).upper() != value for key, value in expected_contract.items()):
            raise IbkrDailyProviderError("IBKR qualified contract does not match the requested USD stock")
        con_id = contract.get("con_id")
        if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0:
            raise IbkrDailyProviderError("IBKR qualified contract is missing a positive conId")

        raw_bars = response.get("bars")
        if not isinstance(raw_bars, list) or not raw_bars:
            raise IbkrDailyProviderError("IBKR returned no historical daily bars")
        rows: list[dict[str, float]] = []
        dates: list[date] = []
        for item in raw_bars:
            if not isinstance(item, dict):
                raise IbkrDailyProviderError("IBKR returned a malformed historical bar")
            day = _parse_ibkr_session_date(item.get("date"))
            if start <= day <= end:
                try:
                    rows.append({
                        key: float(item[key])
                        for key in ("open", "high", "low", "close", "volume")
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    raise IbkrDailyProviderError("IBKR returned an incomplete OHLCV bar") from exc
                dates.append(day)
        if not rows:
            raise IbkrDailyProviderError("IBKR returned no bars inside the requested date range")
        if dates != sorted(set(dates)):
            raise IbkrDailyProviderError("IBKR bars must have unique increasing exchange session dates")

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise IbkrDailyProviderError("host retrieval clock must return a timezone-aware timestamp")
        retrieved_at = retrieved_at.astimezone(timezone.utc)
        frame = pd.DataFrame(rows, index=pd.DatetimeIndex(pd.to_datetime(dates), name="session_date"))
        return IbkrDailyResult(
            symbol=clean_symbol,
            frame=frame,
            provenance={
                "schema": "host-observed-provider-fetch/1",
                "requested_provider": "ibkr",
                "actual_loader_identity": "ibkr-local-tws-api",
                "profile_id": response["profile_id"],
                "environment": response["environment"],
                "request_parameters": {
                    "symbol": clean_symbol,
                    "instrument_id": instrument_id,
                    "start_date": start.isoformat(),
                    "end_date_inclusive": end.isoformat(),
                    "duration": duration,
                    "end_date": end.isoformat(),
                    "bar_size": "1 day",
                    "what_to_show": "TRADES",
                    "use_rth": True,
                    "exchange": "SMART",
                    "currency": "USD",
                    "sec_type": "STK",
                    **({"port": port} if port is not None else {}),
                },
                "retrieved_at": retrieved_at.isoformat(),
                "qualified_contract": contract,
                "bar_count_returned_in_range": len(rows),
                "provider_warning": str(response.get("warning", "")),
                "identity_authentication": "IBKR local API session; selected read-only profile asserted",
                "point_in_time_availability": "UNVERIFIED",
                "adjustment_semantics": "TRADES bars; IBKR adjustment treatment unverified",
                "broker_contract_mapping": "qualified conId recorded; application instrument mapping caller-declared",
            },
        )


def _parse_ibkr_session_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise IbkrDailyProviderError("IBKR historical bar has an invalid session date")


__all__ = ["IbkrDailyProviderError", "IbkrDailyResult", "IbkrDailyProvider"]
