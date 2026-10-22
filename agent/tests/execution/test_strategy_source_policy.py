from __future__ import annotations

import pytest

from src.execution.strategy_source_policy import (
    StrategySourcePolicyError,
    validate_strategy_source,
)


@pytest.mark.parametrize(
    "source",
    [
        "def on_event(event, state): return {'state': state, 'order_proposals': []}\n",
        "import math\ndef on_event(event, state): return {'state': {}, 'order_proposals': []}\n",
        "from statistics import mean\ndef on_event(event, state): return {'state': {}, 'order_proposals': []}\n",
    ],
)
def test_supported_source_policy_accepts_event_strategy_and_allowlisted_imports(source):
    tree = validate_strategy_source(source)
    assert tree.body


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef on_event(event, state): pass\n",
        "from .helpers import f\ndef on_event(event, state): pass\n",
        "def on_event(): pass\n",
        "def on_event(event): pass\n",
        "def on_event(event, state={}): pass\n",
        "def on_event(event, state, *args): pass\n",
        "def on_event(event, state, **kwargs): pass\n",
        "def on_event(event, *, state): pass\n",
        "@decorator\ndef on_event(event, state): pass\n",
        "async def on_event(event, state): pass\n",
        "def on_event(event, state): return eval('1')\n",
        "def on_event(event, state): return event.__class__\n",
        "def on_event(event, state): return __builtins__\n",
    ],
)
def test_source_policy_rejects_unsupported_imports_signatures_and_direct_apis(source):
    with pytest.raises(StrategySourcePolicyError):
        validate_strategy_source(source)
