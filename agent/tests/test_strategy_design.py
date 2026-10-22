"""Fail-closed tests for the bounded natural-language strategy parser."""

from __future__ import annotations

import json

import pytest

from src.execution.strategy_design import configured_model_intent, configured_strategy_intent


def _intent(**overrides) -> str:
    value = {
        "fast_period": 10,
        "slow_period": 30,
        "fast_period_evidence": "10-day fast",
        "slow_period_evidence": "30-day slow",
        "entry_rule": "fast_sma_crosses_above_slow_sma",
        "exit_rule": "fast_sma_crosses_below_slow_sma",
        "direction": "long_only",
        "unsupported_conditions": [],
        "clarification": None,
    }
    value.update(overrides)
    return json.dumps(value)


def _rsi_intent(**overrides) -> str:
    value = {
        "period": 14,
        "entry_below": 30,
        "exit_above": 70,
        "entry_rule": "rsi_crosses_below_entry_threshold",
        "exit_rule": "rsi_crosses_above_exit_threshold",
        "direction": "long_only",
        "period_evidence": "14-period",
        "entry_evidence": "below 30",
        "exit_evidence": "above 70",
        "unsupported_conditions": [],
        "clarification": None,
    }
    value.update(overrides)
    return json.dumps(value)


def test_configured_model_intent_accepts_only_the_bounded_sma_schema():
    assert configured_model_intent(
        "Use a 10-day fast and 30-day slow moving average crossover.",
        intent_generator=lambda _prompt: _intent(),
    ) == (10, 30)


@pytest.mark.parametrize("raw", [
    _intent(fast_period_evidence=None),
    _intent(fast_period=12, fast_period_evidence="10-day fast"),
    _intent(fast_period_evidence="10-day crossover", slow_period_evidence="30-day slow"),
    _intent(fast_period_evidence="10-day fast", slow_period_evidence="30-day fast"),
    _intent(fast_period=20, fast_period_evidence="fast 5-day and slow 20-day"),
    _intent(fast_period_evidence="fast and slow 10-day"),
])
def test_sma_model_intent_requires_source_bound_periods_and_roles(raw):
    with pytest.raises(ValueError):
        configured_model_intent(
            "Use a 10-day fast and 30-day slow moving average crossover; another setup has fast 5-day and slow 20-day averages; the phrase fast and slow 10-day is ambiguous.",
            intent_generator=lambda _prompt: raw,
        )


@pytest.mark.parametrize("description", [
    "Use SMA 10/30 and confirm with EMA 50.",
    "Buy on the crossover with an ATR stop loss.",
    "Only trade when price is above VWAP and volume is high.",
    "Use weekly bars and a 2% risk per trade.",
    "SMA 10/30 with MACD confirmation and a trailing stop.",
    "10日均线上穿30日均线买入，止损5%。",
    "均线金叉后仅在价格高于VWAP且成交量放大时买入。",
    "周线策略，每笔风险1%，使用杠杆。",
])
def test_known_unsupported_condition_prefilter_rejects_without_calling_model(description):
    calls = []

    with pytest.raises(ValueError, match="not supported"):
        configured_model_intent(description, intent_generator=lambda prompt: calls.append(prompt) or _intent())

    assert calls == []


def test_rsi_prefilter_catches_additional_condition_without_calling_model():
    calls = []

    with pytest.raises(ValueError, match="additional condition"):
        configured_strategy_intent(
            "Use RSI(8): buy on a downward cross of 25, exit above 65, with an ATR stop.",
            intent_generator=lambda prompt: calls.append(prompt) or _rsi_intent(),
        )

    assert calls == []


@pytest.mark.parametrize(
    "raw",
    [
        _intent(unsupported_conditions=["RSI below 20"]),
        _intent(fast_period=None, clarification="Which fast period?"),
        _intent(entry_rule="buy when RSI is oversold"),
        _intent(exit_rule="sell at a 5% loss"),
        _intent(direction="short_only"),
        _intent(unexpected_field="must be rejected"),
        "Here is the JSON: " + _intent(),
    ],
    ids=["unsupported-condition", "missing-period", "alternate-entry", "alternate-exit", "short-direction", "extra-key", "non-json-wrapper"],
)
def test_configured_model_intent_fails_closed_on_unsupported_or_invalid_intent(raw):
    with pytest.raises(ValueError):
        configured_model_intent(
            "A prose SMA request with no exact legacy grammar.",
            intent_generator=lambda _prompt: raw,
        )


@pytest.mark.parametrize("sentence", [
    "Buy when 14-period RSI crosses below 30; exit when RSI crosses above 70.",
    "RSI(14)下穿30买入，RSI上穿70卖出",
])
def test_configured_strategy_intent_accepts_complete_explicit_rsi(sentence):
    assert configured_strategy_intent(sentence) == {
        "template_id": "rsi_threshold",
        "parameters": {"period": 14, "entry_below": 30.0, "exit_above": 70.0},
    }


@pytest.mark.parametrize("sentence", [
    "Buy when RSI is below 20",
    "Buy when RSI crosses below 30; exit when RSI crosses above 70.",
    "Buy when 14-period RSI crosses below 30; exit when RSI crosses above 70 and 10-day SMA crosses above 30-day SMA.",
    "Buy when 14-period RSI crosses below 80; exit when RSI crosses above 20.",
])
def test_configured_strategy_intent_rejects_incomplete_or_mixed_rsi(sentence):
    def ask_for_missing_values(_prompt):
        return _rsi_intent(
            period=None, entry_below=None, exit_above=None, entry_rule=None,
            exit_rule=None, direction=None, clarification="Please specify period and both thresholds.",
        )

    with pytest.raises(ValueError):
        configured_strategy_intent(sentence, intent_generator=ask_for_missing_values)


def test_configured_strategy_intent_extracts_bounded_rsi_from_flexible_prose():
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return _rsi_intent(
            period=8, entry_below=25, exit_above=65,
            period_evidence="RSI(8)", entry_evidence="downward cross of 25",
            exit_evidence="above 65",
        )

    result = configured_strategy_intent(
        "Use RSI(8): buy on a downward cross of 25 and close above 65.",
        intent_generator=generate,
    )

    assert result == {
        "template_id": "rsi_threshold",
        "parameters": {"period": 8, "entry_below": 25.0, "exit_above": 65.0},
    }
    assert len(prompts) == 1
    assert "Do not assume or invent period or threshold defaults" in prompts[0]


@pytest.mark.parametrize("raw", [
    _rsi_intent(period=None, entry_below=None, clarification="What period and thresholds?"),
    _rsi_intent(unsupported_conditions=["stop loss"]),
    _rsi_intent(entry_rule="rsi_is_below_entry_threshold"),
    _rsi_intent(exit_rule="rsi_crosses_below_exit_threshold"),
    _rsi_intent(direction="short_only"),
    _rsi_intent(entry_below=80, exit_above=20),
    _rsi_intent(unexpected_field="reject me"),
    _rsi_intent(period_evidence="8", entry_evidence="below 25", exit_evidence="above 65"),
    _rsi_intent(entry_evidence="above 30"),
    "prefix " + _rsi_intent(),
])
def test_rsi_model_intent_rejects_missing_or_unsupported_semantics(raw):
    with pytest.raises(ValueError):
        configured_strategy_intent(
            "Use RSI for a long-only strategy.", intent_generator=lambda _prompt: raw,
        )
