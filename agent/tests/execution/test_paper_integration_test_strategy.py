from __future__ import annotations

from pathlib import Path

import backtrader
import pytest

from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION
from src.execution.paper_integration_test_config import PaperIntegrationTestConfig
from src.execution.paper_integration_test_strategy import (
    STRATEGY_ID,
    build_paper_integration_test_spec,
    prepare_paper_integration_test_strategy,
)
from src.execution.versions import StrategyVersionStore
from src.execution.strategy_source_policy import validate_strategy_source
from tests.execution.test_paper_integration_test_config import _payload


def test_prepares_immutable_one_shot_lmt_strategy_for_session_before_window(tmp_path: Path):
    config = PaperIntegrationTestConfig.model_validate(_payload())
    store = StrategyVersionStore(tmp_path / "versions")

    record = prepare_paper_integration_test_strategy(config, store=store)
    reloaded = store.get_version(record.version_id)
    source = store.read_upload_source(record.version_id).decode("utf-8")

    assert record is not None
    assert reloaded.version_id == reloaded.version_hash
    assert reloaded.strategy_id == STRATEGY_ID
    assert reloaded.spec.execution_policy.order_type == "limit"
    assert reloaded.spec.execution_policy.time_in_force == "DAY"
    assert reloaded.spec.instrument_ids == ("AAPL",)
    assert reloaded.spec.parameters["signal_session_date"] == "2026-10-30"
    assert "'2026-10-30'" in source
    assert '"limit_price": "400.00"' in source
    validate_strategy_source(source)
    assert reloaded.engine_version == f"backtrader/{backtrader.__version__}"
    assert reloaded.compiler_version == PYTHON_WORKER_COMPILER_VERSION
    assert prepare_paper_integration_test_strategy(config, store=store).version_id == record.version_id


def test_generated_strategy_rejects_datetime_signal_date():
    from datetime import datetime, timezone

    with pytest.raises(TypeError, match="not a datetime"):
        build_paper_integration_test_spec(datetime(2026, 10, 30, tzinfo=timezone.utc))
