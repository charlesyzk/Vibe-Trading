from datetime import date
from types import SimpleNamespace

import pytest

from src.execution.paper_strategy_warmup import build_paper_warmup, verify_current_signal_bar
import src.execution.paper_strategy_warmup as warmup_module
from tests.execution.test_outbox_dispatch import _bar


def test_signal_requires_latest_matching_gateway_bar():
    signal = _bar()
    gateway = signal.model_copy(update={"source": "gateway", "adjustment": "unverified"})
    assert verify_current_signal_bar(signal_bar=signal, gateway_bars=(gateway,)) == gateway
    with pytest.raises(ValueError, match="latest completed"):
        verify_current_signal_bar(signal_bar=signal, gateway_bars=(
            gateway, gateway.model_copy(update={"session_date": date(2026, 12, 31)})))
    with pytest.raises(ValueError, match="differs"):
        verify_current_signal_bar(signal_bar=signal, gateway_bars=(gateway.model_copy(update={"close": signal.close + 1}),))
    with pytest.raises(ValueError, match="strictly ordered"):
        verify_current_signal_bar(signal_bar=signal, gateway_bars=(gateway, gateway))
    with pytest.raises(ValueError, match="unavailable"):
        verify_current_signal_bar(signal_bar=signal, gateway_bars=())


def test_warmup_replays_prior_bars_without_dispatching_final_signal(tmp_path, monkeypatch):
    version = "a" * 64
    bars = tuple(_bar().model_copy(update={"session_date": day}) for day in (
        date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)))
    manifest = SimpleNamespace(evaluation_end=date(2026, 10, 30), instrument_id=bars[0].instrument_id)
    monkeypatch.setattr(warmup_module, "load_python_upload_resume_inputs", lambda _: {
        "version_id": version, "strategy_version_hash": version,
        "market_data": SimpleNamespace(manifest=manifest, bar_records=bars),
    })
    monkeypatch.setattr(warmup_module, "canonical_sha256", lambda _: "b" * 64)
    monkeypatch.setattr(warmup_module, "StrategyVersionStore", lambda _: SimpleNamespace(
        read_upload_source=lambda _version: b"def on_event(event, state): pass"))
    calls = []
    def worker(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(state={"count": len(calls)}, order_proposals=[{"ignored": True}])
    monkeypatch.setattr(warmup_module, "invoke_strategy_worker", worker)
    result = build_paper_warmup(runtime_root=tmp_path, run_id="run", version_id=version,
                                expected_manifest_hash="b" * 64, sandbox=object())
    assert [call["bar"].session_date for call in calls] == [date(2026, 10, 28), date(2026, 10, 29)]
    assert calls[1]["state"] == {"count": 1}
    assert result.checkpoint == {"count": 2} and result.signal_bar == bars[-1]
    with pytest.raises(ValueError, match="data manifest"):
        build_paper_warmup(runtime_root=tmp_path, run_id="run", version_id=version,
                           expected_manifest_hash="c" * 64, sandbox=object())
    with pytest.raises(ValueError, match="run ID"):
        build_paper_warmup(runtime_root=tmp_path, run_id="../run", version_id=version,
                           expected_manifest_hash="b" * 64, sandbox=object())
