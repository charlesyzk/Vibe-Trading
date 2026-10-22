from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from src.execution.contracts import Bar, DataManifest
from src.execution.market_data_contracts import MarketDataSnapshot


def _snapshot_values(*, fetched_at: datetime | None = None):
    days = (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22))
    bars = tuple(
        Bar(
            instrument_id="us_equity_demo",
            session_date=day,
            start_at=datetime.combine(day, time(13, 30), tzinfo=timezone.utc),
            end_at=datetime.combine(day, time(20), tzinfo=timezone.utc),
            available_at=datetime.combine(day, time(20, 1), tzinfo=timezone.utc),
            timezone="America/New_York",
            open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
            close=Decimal("10"), volume=Decimal("100"), currency="USD",
            is_final=True, source="fixture-provider", adjustment="unadjusted",
        )
        for day in days
    )
    manifest = DataManifest(
        manifest_id="snapshot-fixture",
        provider="fixture-provider",
        instrument_id="us_equity_demo",
        currency="USD",
        fields=("open", "high", "low", "close", "volume"),
        range_start=days[0], range_end=days[-1],
        availability_model="completed bar available after close",
        calendar_version="fixture-calendar-v1",
        adjustment_policy="unadjusted",
        corporate_action_policy="fixture-only",
        download_parameters={},
        fetched_at=fetched_at or datetime(2026, 10, 22, 20, 2, tzinfo=timezone.utc),
        file_sha256="a" * 64,
        quality_report={"validation": "passed"},
        warmup_start=days[0], warmup_end=days[1],
        evaluation_start=days[2], evaluation_end=days[-1],
    )
    return bars, manifest


def test_market_data_snapshot_matches_manifest_time_range_and_identity():
    bars, manifest = _snapshot_values()

    snapshot = MarketDataSnapshot(bars=bars, manifest=manifest)

    assert snapshot.bars == bars


def test_market_data_snapshot_rejects_bar_available_after_fetch_time():
    bars, manifest = _snapshot_values()
    manifest = manifest.model_copy(update={"fetched_at": datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)})

    with pytest.raises(ValueError, match="fetched_at cannot precede"):
        MarketDataSnapshot(bars=bars, manifest=manifest)


def test_market_data_snapshot_rejects_bars_outside_manifest_range():
    bars, manifest = _snapshot_values()
    manifest = manifest.model_copy(update={
        "range_end": date(2026, 10, 21),
        "evaluation_end": date(2026, 10, 21),
    })

    with pytest.raises(ValueError, match="span the manifest data range"):
        MarketDataSnapshot(bars=bars, manifest=manifest)


@pytest.mark.parametrize(
    ("bar_update", "message"),
    [
        ({"currency": "EUR"}, "currency must match"),
        ({"source": "unrelated-provider"}, "source must match"),
        ({"adjustment": "split-adjusted"}, "adjustment must match"),
        ({"is_final": False}, "require completed bars"),
    ],
)
def test_market_data_snapshot_rejects_manifest_identity_mismatch(bar_update, message):
    bars, manifest = _snapshot_values()
    altered = bars[-1].model_copy(update=bar_update)

    with pytest.raises(ValueError, match=message):
        MarketDataSnapshot(bars=(*bars[:-1], altered), manifest=manifest)


def test_data_manifest_v10_remains_readable_without_currency():
    _, manifest = _snapshot_values()
    legacy = DataManifest.model_validate({
        **manifest.model_dump(mode="json"),
        "schema_version": "1.0",
        "currency": None,
    })

    assert legacy.currency is None
