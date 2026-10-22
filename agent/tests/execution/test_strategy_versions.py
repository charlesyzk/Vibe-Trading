"""Focused tests for deterministic immutable strategy version storage."""

import hashlib
import json

import pytest

from src.execution.contracts import SourceKind, StrategySpec, canonical_sha256
from src.execution.versions import (
    InvalidStrategySource,
    ImmutableVersionConflict,
    StrategyVersionStore,
    VersionStoreError,
)


def _spec(*, source_kind: SourceKind = SourceKind.NATURAL_LANGUAGE_TEMPLATE, **changes) -> StrategySpec:
    if source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE:
        body = {
            "schema_version": "1.0",
            "strategy_id": "sma_demo",
            "source_kind": source_kind.value,
            "template_id": "sma_cross",
            "template_version": "1.0",
            "instrument_ids": ["us_equity_demo"],
            "bar_policy": {
                "timeframe": "1d",
                "session": "regular",
                "completed_only": True,
                "calendar_id": "us_equity_rth",
                "timezone": "America/New_York",
            },
            "parameters": {"fast_period": 10, "slow_period": 30},
            "position_policy": {
                "mode": "target_fraction",
                "long_only": True,
                "whole_shares": True,
                "allocation_fraction": "0.20",
            },
            "execution_policy": {
                "decision": "completed_bar_available",
                "dispatch": "next_regular_session",
                "order_type": "market",
                "time_in_force": "DAY",
                "outside_rth": False,
                "gap_action": "reject_and_pause",
                "missed_session_action": "expire_and_pause",
            },
        }
    else:
        body = {
            "schema_version": "1.0",
            "strategy_id": "uploaded_demo",
            "source_kind": source_kind.value,
            "source_artifact_id": "upload-1",
            "instrument_ids": ["us_equity_demo"],
            "bar_policy": {
                "timeframe": "1d",
                "session": "regular",
                "completed_only": True,
                "calendar_id": "us_equity_rth",
                "timezone": "America/New_York",
            },
            "parameters": {},
            "position_policy": {"mode": "explicit_quantity", "long_only": True, "whole_shares": True},
            "execution_policy": {
                "decision": "completed_bar_available",
                "dispatch": "next_regular_session",
                "order_type": "market",
                "time_in_force": "DAY",
                "outside_rth": False,
                "gap_action": "reject_and_pause",
                "missed_session_action": "expire_and_pause",
            },
        }
    body.update(changes)
    return StrategySpec.model_validate(body)


def test_template_version_hashes_spec_template_source_and_runtime_versions(tmp_path):
    store = StrategyVersionStore(tmp_path)
    spec = _spec()
    source = b"trusted-template-source-v1\n"

    first = store.create_version(
        spec,
        template_source=source,
        engine_version="backtrader/1.9.78.123",
        compiler_version="vibe-bt-compiler/1",
    )
    again = store.create_version(
        spec,
        template_source=source,
        engine_version="backtrader/1.9.78.123",
        compiler_version="vibe-bt-compiler/1",
    )
    source_changed = store.create_version(
        spec,
        template_source=b"trusted-template-source-v2\n",
        engine_version="backtrader/1.9.78.123",
        compiler_version="vibe-bt-compiler/1",
    )
    engine_changed = store.create_version(
        spec,
        template_source=source,
        engine_version="backtrader/1.9.78.124",
        compiler_version="vibe-bt-compiler/1",
    )

    assert first.version_id == again.version_id
    assert first.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE
    assert first.spec_hash == canonical_sha256(spec)
    assert first.source_hash == hashlib.sha256(source).hexdigest()
    assert first.upload_artifact is None
    assert first.template_artifact == f"templates/{first.source_hash}.py"
    assert store.read_template_source(first.version_id) == source
    assert source_changed.version_id != first.version_id
    assert engine_changed.version_id != first.version_id
    assert len(list((tmp_path / "sources").glob("*"))) == 0
    assert len(list((tmp_path / "templates").glob("*"))) == 2


def test_upload_source_is_persisted_byte_exact_and_versioned(tmp_path):
    store = StrategyVersionStore(tmp_path)
    source = b"# strategy\r\nclass UserStrategy: pass\r\n\xff"

    record = store.create_version(
        _spec(source_kind=SourceKind.PYTHON_UPLOAD),
        upload_source=source,
        engine_version="backtrader/1.9.78.123",
        compiler_version="vibe-python-template/1",
    )

    assert record.source_kind is SourceKind.PYTHON_UPLOAD
    assert record.source_hash == hashlib.sha256(source).hexdigest()
    assert record.upload_artifact == f"sources/{record.source_hash}.py"
    assert store.read_upload_source(record.version_id) == source
    loaded = store.get_version(record.version_id)
    assert loaded == record


def test_nested_spec_mutation_after_create_does_not_mutate_persisted_version(tmp_path):
    store = StrategyVersionStore(tmp_path)
    spec = _spec()
    record = store.create_version(
        spec,
        template_source=b"trusted-template-source\n",
        engine_version="bt/1",
        compiler_version="compiler/1",
    )
    spec.parameters["fast_period"] = 99

    loaded = store.get_version(record.version_id)
    assert loaded.spec.parameters["fast_period"] == 10
    assert loaded.spec_hash == record.spec_hash


@pytest.mark.parametrize(
    ("source_kind", "kwargs"),
    [
        (SourceKind.NATURAL_LANGUAGE_TEMPLATE, {"upload_source": b"x"}),
        (SourceKind.PYTHON_UPLOAD, {"template_source": b"x"}),
        (SourceKind.NATURAL_LANGUAGE_TEMPLATE, {}),
    ],
)
def test_source_kind_requires_exactly_its_source_input(tmp_path, source_kind, kwargs):
    with pytest.raises(InvalidStrategySource):
        StrategyVersionStore(tmp_path).create_version(
            _spec(source_kind=source_kind),
            engine_version="bt/1",
            compiler_version="compiler/1",
            **kwargs,
        )


def test_upload_size_limit_includes_exact_boundary_and_rejects_one_byte_over(tmp_path):
    store = StrategyVersionStore(tmp_path, max_source_bytes=8)
    exact = store.create_version(
        _spec(source_kind=SourceKind.PYTHON_UPLOAD),
        upload_source=b"12345678",
        engine_version="bt/1",
        compiler_version="compiler/1",
    )
    assert store.read_upload_source(exact.version_id) == b"12345678"

    with pytest.raises(InvalidStrategySource, match="8-byte source limit"):
        store.create_version(
            _spec(source_kind=SourceKind.PYTHON_UPLOAD),
            upload_source=b"123456789",
            engine_version="bt/1",
            compiler_version="compiler/1",
        )


@pytest.mark.parametrize("source", [b"", bytearray(b"x"), "x"])
def test_upload_source_must_be_nonempty_bytes(tmp_path, source):
    with pytest.raises(InvalidStrategySource):
        StrategyVersionStore(tmp_path).create_version(
            _spec(source_kind=SourceKind.PYTHON_UPLOAD),
            upload_source=source,
            engine_version="bt/1",
            compiler_version="compiler/1",
        )


def test_existing_version_record_is_never_replaced_or_silently_repaired(tmp_path):
    store = StrategyVersionStore(tmp_path)
    record = store.create_version(
        _spec(),
        template_source=b"trusted-template-source\n",
        engine_version="bt/1",
        compiler_version="compiler/1",
    )
    path = tmp_path / "versions" / f"{record.version_id}.json"
    original = path.read_bytes()
    path.write_bytes(original + b" ")

    with pytest.raises(ImmutableVersionConflict, match="different bytes"):
        store.create_version(
            _spec(),
            template_source=b"trusted-template-source\n",
            engine_version="bt/1",
            compiler_version="compiler/1",
        )
    assert path.read_bytes() == original + b" "


def test_uploaded_blob_tampering_is_detected(tmp_path):
    store = StrategyVersionStore(tmp_path)
    record = store.create_version(
        _spec(source_kind=SourceKind.PYTHON_UPLOAD),
        upload_source=b"class UserStrategy: pass\n",
        engine_version="bt/1",
        compiler_version="compiler/1",
    )
    source_path = tmp_path / record.upload_artifact
    source_path.write_bytes(b"different bytes\n")

    with pytest.raises(VersionStoreError, match="source bytes do not match"):
        store.get_version(record.version_id)


def test_trusted_template_blob_tampering_is_detected(tmp_path):
    store = StrategyVersionStore(tmp_path)
    record = store.create_version(
        _spec(),
        template_source=b"trusted-template-source\n",
        engine_version="bt/1",
        compiler_version="compiler/1",
    )
    (tmp_path / record.template_artifact).write_bytes(b"different bytes\n")

    with pytest.raises(VersionStoreError, match="source bytes do not match"):
        store.get_version(record.version_id)
