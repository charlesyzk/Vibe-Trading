from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pandas as pd

from src.bt_runtime.market_data import (
    CSV_COLUMNS,
    MarketDataMetadata,
    MarketSession,
    canonical_daily_bars_sha256,
    import_completed_daily_csv,
)
from src.bt_runtime.calendars import build_xnys_schedule
from src.bt_runtime.strategies.sma import run_verified_backtest
from src.execution.contracts import canonical_sha256


def make_fixture():
    days = [date(2025, 2, day) for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 18, 19)]
    sessions = [
        MarketSession(
            day,
            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=14, minute=30),
            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21),
        )
        for day in days
    ]
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    rows = [CSV_COLUMNS]
    for session, close in zip(sessions, closes, strict=True):
        op = close + 0.25
        available = session.close_at + timedelta(minutes=1)
        rows.append((
            session.session_date.isoformat(), session.open_at.isoformat(), session.close_at.isoformat(),
            available.isoformat(), f"{op:.2f}", f"{max(op, close) + 1:.2f}",
            f"{min(op, close) - 1:.2f}", f"{close:.2f}", "100000",
        ))
    raw = ("\n".join(",".join(map(str, row)) for row in rows) + "\n").encode()
    metadata = MarketDataMetadata(
        provider="fixture_vendor", instrument_id="AAPL", currency="USD", timezone="America/New_York",
        calendar_id="fixture_calendar", calendar_version="2025.1",
        availability_model="completed_bar_available; vendor + 1 minute",
        adjustment_policy="split_adjusted", corporate_action_policy="vendor_adjustments",
        fetched_at=datetime(2025, 2, 20, tzinfo=timezone.utc),
        warmup_start=days[0], warmup_end=days[1], evaluation_start=days[2], evaluation_end=days[-1],
        download_parameters={"interval": "1d", "auto_adjust": True},
    )
    return raw, metadata, sessions


def test_import_completed_bars_hashes_bytes_and_matches_schedule():
    raw, metadata, sessions = make_fixture()
    imported = import_completed_daily_csv(raw, metadata, sessions)

    assert imported.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert imported.manifest.file_sha256 == imported.raw_sha256
    assert imported.manifest.synthetic is False
    assert imported.manifest.calendar_version == "fixture_calendar@2025.1"
    assert imported.manifest.quality_report["session_schedule"] == "exact_match"
    assert imported.manifest.quality_report["strategy_input_schema"] == "vibe-daily-ohlcv/1"
    assert imported.manifest.quality_report["strategy_input_sha256"] == canonical_daily_bars_sha256(imported.bars)
    assert imported.manifest.quality_report["provider_attestation"] == "caller_declared_unverified"
    assert imported.bar_records[0].available_at == imported.bars.index[0].to_pydatetime()
    assert len(imported.bars) == len(sessions)


def test_import_rejects_fetch_time_before_latest_bar_availability():
    raw, metadata, sessions = make_fixture()
    with pytest.raises(ValueError, match="fetched_at cannot precede"):
        import_completed_daily_csv(
            raw,
            metadata.model_copy(update={"fetched_at": sessions[-1].close_at}),
            sessions,
        )


def test_import_allows_fetch_time_equal_to_latest_bar_availability():
    raw, metadata, sessions = make_fixture()
    latest_available_at = sessions[-1].close_at + timedelta(minutes=1)
    imported = import_completed_daily_csv(
        raw,
        metadata.model_copy(update={"fetched_at": latest_available_at}),
        sessions,
    )

    assert imported.manifest.fetched_at == latest_available_at


def test_verified_runner_uses_imported_manifest_and_real_backtrader():
    raw, metadata, sessions = make_fixture()
    imported = import_completed_daily_csv(raw, metadata, sessions)
    spec = {
        "schema_version": "1.0", "strategy_id": "aapl_sma", "source_kind": "natural_language_template",
        "template_id": "sma_cross", "template_version": "1.0", "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
        "parameters": {"fast_period": 2, "slow_period": 3},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                             "allocation_fraction": "0.50", "rebalance": "on_signal_transition",
                             "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
    }
    report = run_verified_backtest(
        spec, imported.bars, imported.manifest,
        strategy_budget=Decimal("40000"), fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"),
    )
    assert report.provenance["data_source"] == "provider_import"
    assert report.provenance["data_authenticity"] == "caller_declared_unverified"
    assert report.data_manifest["synthetic"] is False
    assert report.data_manifest_hash == canonical_sha256(imported.manifest)
    assert report.engine == "Backtrader 1.9.78.123 / BackBroker"
    assert report.fills
    slipped = run_verified_backtest(
        spec, imported.bars, imported.manifest,
        strategy_budget=Decimal("40000"), fee_reserve=Decimal("25"),
        price_buffer_fraction=Decimal("0.01"), slippage_fraction=0.01,
    )
    assert slipped.provenance["cost"]["slippage_fraction"] == "0.01"
    assert slipped.fills[0]["price"] > report.fills[0]["price"]


def test_verified_runner_rejects_same_range_bars_or_digest_tampered_after_import():
    raw, metadata, sessions = make_fixture()
    imported = import_completed_daily_csv(raw, metadata, sessions)
    spec = {
        "schema_version": "1.0", "strategy_id": "aapl_sma", "source_kind": "natural_language_template",
        "template_id": "sma_cross", "template_version": "1.0", "instrument_ids": ["AAPL"],
        "bar_policy": {"timeframe": "1d", "session": "regular", "completed_only": True,
                       "calendar_id": "fixture_calendar", "timezone": "America/New_York"},
        "parameters": {"fast_period": 2, "slow_period": 3},
        "position_policy": {"mode": "target_fraction", "long_only": True, "whole_shares": True,
                             "allocation_fraction": "0.50", "rebalance": "on_signal_transition",
                             "initial_entry": "wait_for_cross"},
        "execution_policy": {"decision": "completed_bar_available", "dispatch": "next_regular_session",
                             "order_type": "market", "time_in_force": "DAY", "outside_rth": False,
                             "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"},
    }
    changed = imported.bars.copy()
    changed.iloc[0, changed.columns.get_loc("close")] += 0.01
    with pytest.raises(ValueError, match="strategy_input_sha256"):
        run_verified_backtest(
            spec, changed, imported.manifest,
            strategy_budget=Decimal("40000"), fee_reserve=Decimal("25"),
            price_buffer_fraction=Decimal("0.01"),
        )

    forged_manifest = imported.manifest.model_copy(update={
        "quality_report": {**imported.manifest.quality_report, "strategy_input_sha256": "0" * 64},
    })
    with pytest.raises(ValueError, match="strategy_input_sha256"):
        run_verified_backtest(
            spec, imported.bars, forged_manifest,
            strategy_budget=Decimal("40000"), fee_reserve=Decimal("25"),
            price_buffer_fraction=Decimal("0.01"),
        )


def test_import_rejects_missing_session_duplicate_and_naive_timestamp():
    raw, metadata, sessions = make_fixture()
    lines = raw.decode().splitlines()
    with pytest.raises(ValueError, match="coverage mismatch"):
        import_completed_daily_csv(("\n".join(lines[:-1]) + "\n").encode(), metadata, sessions)
    with pytest.raises(ValueError, match="strictly increasing"):
        import_completed_daily_csv(("\n".join(lines + [lines[1]]) + "\n").encode(), metadata, sessions)
    changed = lines.copy()
    fields = changed[1].split(",")
    fields[1] = "2025-02-03T14:30:00"
    changed[1] = ",".join(fields)
    with pytest.raises(ValueError, match="UTC offset"):
        import_completed_daily_csv(("\n".join(changed) + "\n").encode(), metadata, sessions)
    mismatched_calendar_hash = metadata.model_copy(update={"calendar_schedule_sha256": "f" * 64})
    with pytest.raises(ValueError, match="calendar schedule hash does not match"):
        import_completed_daily_csv(raw, mismatched_calendar_hash, sessions)


def test_import_rejects_session_schedule_bound_to_the_wrong_local_date():
    raw, metadata, sessions = make_fixture()
    shifted_session = MarketSession(
        sessions[0].session_date,
        sessions[0].open_at + timedelta(days=1),
        sessions[0].close_at + timedelta(days=1),
    )
    shifted_sessions = [shifted_session, *sessions[1:]]
    lines = raw.decode().splitlines()
    fields = lines[1].split(",")
    for index in (1, 2, 3):
        fields[index] = (datetime.fromisoformat(fields[index]) + timedelta(days=1)).isoformat()
    lines[1] = ",".join(fields)

    with pytest.raises(ValueError, match="calendar session timestamps do not fall on"):
        import_completed_daily_csv(("\n".join(lines) + "\n").encode(), metadata, shifted_sessions)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("warmup_start", date(2025, 2, 4), "must span the imported market sessions"),
        ("warmup_end", date(2025, 2, 8), "warmup_end must be an imported market session"),
        ("evaluation_start", date(2025, 2, 8), "evaluation_start must be an imported market session"),
        ("evaluation_end", date(2025, 2, 18), "must span the imported market sessions"),
    ],
    ids=["warmup-start-skips-first-bar", "warmup-end-is-weekend", "evaluation-start-is-weekend", "evaluation-end-skips-last-bar"],
)
def test_import_rejects_manifest_window_not_anchored_to_imported_sessions(field, value, message):
    raw, metadata, sessions = make_fixture()
    changed_metadata = metadata.model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        import_completed_daily_csv(raw, changed_metadata, sessions)


def test_import_rejects_invalid_ohlcv_and_nonfinite_decimal():
    raw, metadata, sessions = make_fixture()
    lines = raw.decode().splitlines()
    invalid = lines.copy()
    fields = invalid[1].split(",")
    fields[5] = "1"  # high is below open/close
    invalid[1] = ",".join(fields)
    with pytest.raises(ValueError, match="invalid completed bar"):
        import_completed_daily_csv(("\n".join(invalid) + "\n").encode(), metadata, sessions)
    invalid = lines.copy()
    fields = invalid[1].split(",")
    fields[4] = "Infinity"
    invalid[1] = ",".join(fields)
    with pytest.raises(ValueError, match="finite"):
        import_completed_daily_csv(("\n".join(invalid) + "\n").encode(), metadata, sessions)


def test_pinned_xnys_schedule_handles_dst_holiday_half_day_and_same_day_range():
    spring = build_xnys_schedule(date(2025, 3, 7), date(2025, 3, 10))
    assert [session.session_date for session in spring.sessions] == [date(2025, 3, 7), date(2025, 3, 10)]
    assert [session.open_at.hour for session in spring.sessions] == [14, 13]
    assert [session.close_at.hour for session in spring.sessions] == [21, 20]
    assert all(not session.early_close for session in spring.sessions)

    thanksgiving = build_xnys_schedule(date(2025, 11, 27), date(2025, 11, 28))
    assert [session.session_date for session in thanksgiving.sessions] == [date(2025, 11, 28)]
    assert thanksgiving.sessions[0].early_close is True
    assert thanksgiving.sessions[0].close_at.hour == 18

    juneteenth = build_xnys_schedule(date(2025, 6, 19), date(2025, 6, 19))
    assert juneteenth.sessions == ()
    assert build_xnys_schedule(date(2025, 3, 7), date(2025, 3, 10)).sha256 == spring.sha256


def test_pinned_xnys_provider_route_rejects_missing_session(monkeypatch, tmp_path):
    import backtest.runner as runner
    from agent.tests.execution.test_execution_backend_routing import _backtrader_config, _fake_yfinance_result

    schedule = build_xnys_schedule(date(2025, 2, 3), date(2025, 2, 19))
    sessions = list(schedule.sessions)
    closes = [10, 9, 8, 9, 10, 12, 11, 8, 7, 6, 9, 12]
    missing_index = 5
    retained = [index for index in range(len(sessions)) if index != missing_index]
    missing_session_bars = pd.DataFrame(
        {
            "open": [float(closes[index]) + 0.25 for index in retained],
            "high": [float(closes[index]) + 1 for index in retained],
            "low": [float(closes[index]) - 1 for index in retained],
            "close": [float(closes[index]) for index in retained],
            "volume": [100_000] * len(retained),
        },
        index=pd.DatetimeIndex([
            pd.Timestamp(session.session_date).tz_localize("America/New_York")
            for index, session in enumerate(sessions) if index != missing_index
        ]),
    )
    monkeypatch.setattr(
        runner, "fetch_exact_yfinance_daily",
        lambda symbol, _start, _end: _fake_yfinance_result(missing_session_bars, symbol),
    )
    run_dir = tmp_path / "runtime" / "runs" / "missing_session"
    run_dir.mkdir(parents=True)
    config = _backtrader_config()
    config.update(start_date="2025-02-03", end_date="2025-02-19")
    with pytest.raises(ValueError, match="pinned XNYS calendar; missing="):
        runner.run_backtrader_backend(config, run_dir)
