"""Content-addressed, immutable strategy version storage.

This module stores only the approved StrategySpec snapshot and, for uploaded
Python strategies, the exact source bytes. It does not compile or execute
strategy code.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from src.execution.contracts import (
    SourceKind,
    StrategySpec,
    canonical_json,
    canonical_sha256,
    is_sha256,
)
from src.execution.contracts.models import StrictModel


DEFAULT_MAX_SOURCE_BYTES = 256 * 1024
_MAX_VERSION_LABEL_CHARS = 128


class VersionStoreError(RuntimeError):
    """Base error for invalid or inconsistent version store operations."""


class InvalidStrategySource(VersionStoreError, ValueError):
    """Raised when source bytes do not match the StrategySpec source kind."""


class ImmutableVersionConflict(VersionStoreError):
    """An existing content-addressed record differs from the requested value."""


class StrategyVersionRecord(StrictModel):
    """Immutable index record for one exact strategy execution version."""

    schema_version: Literal["1.0"] = "1.0"
    version_id: str
    version_hash: str
    strategy_id: str
    source_kind: SourceKind
    spec: StrategySpec
    spec_hash: str
    source_hash: str
    engine_version: str
    compiler_version: str
    upload_artifact: str | None = None
    template_artifact: str | None = None

    @classmethod
    def model_validate_record(cls, value: object) -> "StrategyVersionRecord":
        """Parse and verify a stored record's internal hashes and source shape."""
        record = cls.model_validate(value)
        if not is_sha256(record.version_hash) or record.version_id != record.version_hash:
            raise VersionStoreError("stored strategy version id/hash is invalid")
        if not is_sha256(record.spec_hash) or canonical_sha256(record.spec) != record.spec_hash:
            raise VersionStoreError("stored strategy spec hash does not match")
        if not is_sha256(record.source_hash):
            raise VersionStoreError("stored strategy source hash is invalid")
        if record.spec.strategy_id != record.strategy_id or record.spec.source_kind is not record.source_kind:
            raise VersionStoreError("stored strategy identity does not match its spec")
        if record.source_kind is SourceKind.PYTHON_UPLOAD:
            if record.upload_artifact != f"sources/{record.source_hash}.py" or record.template_artifact is not None:
                raise VersionStoreError("stored upload artifact path is invalid")
        elif record.upload_artifact is not None or record.template_artifact != f"templates/{record.source_hash}.py":
            raise VersionStoreError("template source artifact path is invalid")
        expected_hash = _version_hash(
            record.spec,
            record.source_hash,
            record.engine_version,
            record.compiler_version,
        )
        if expected_hash != record.version_hash:
            raise VersionStoreError("stored strategy version hash does not match its inputs")
        return record


def _version_hash(
    spec: StrategySpec,
    source_hash: str,
    engine_version: str,
    compiler_version: str,
) -> str:
    return canonical_sha256(
        {
            "schema_version": "strategy-version-hash/1",
            "spec": spec,
            "source_hash": source_hash,
            "engine_version": engine_version,
            "compiler_version": compiler_version,
        }
    )


def _validate_label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_VERSION_LABEL_CHARS:
        raise ValueError(f"{name} must be a non-empty string of at most {_MAX_VERSION_LABEL_CHARS} characters")
    return value.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StrategyVersionStore:
    """Persist immutable, deterministic strategy versions under a directory.

    ``template_source`` is the exact trusted source bundle selected by the
    template registry. It is content-addressed and persisted too, so a version
    can be reconstructed after templates change. ``upload_source`` is caller
    supplied Python and is persisted byte-for-byte after the strict size check.
    """

    def __init__(self, root: str | Path, *, max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES):
        if type(max_source_bytes) is not int or max_source_bytes <= 0:
            raise ValueError("max_source_bytes must be a positive integer")
        self.root = Path(root).expanduser().resolve()
        self.max_source_bytes = max_source_bytes

    def create_version(
        self,
        spec: StrategySpec,
        *,
        engine_version: str,
        compiler_version: str,
        upload_source: bytes | None = None,
        template_source: bytes | None = None,
    ) -> StrategyVersionRecord:
        """Create or retrieve a version, rejecting attempts to mutate records.

        Repeating the same complete input is idempotent and returns the
        existing immutable record. Any changed spec/source/runtime version
        computes a different version hash and therefore a new record.
        """
        if not isinstance(spec, StrategySpec):
            raise TypeError("spec must be a validated StrategySpec")
        engine_version = _validate_label(engine_version, "engine_version")
        compiler_version = _validate_label(compiler_version, "compiler_version")

        # Round-trip through canonical JSON to detach nested parameter dicts
        # from the caller; Pydantic's frozen flag does not freeze nested dicts.
        frozen_spec = StrategySpec.model_validate(json.loads(canonical_json(spec)))
        if frozen_spec.source_kind is SourceKind.PYTHON_UPLOAD:
            if upload_source is None or template_source is not None:
                raise InvalidStrategySource("Python upload versions require upload_source only")
            source_bytes = self._validate_source_bytes(upload_source, "upload_source")
            upload_artifact = f"sources/{_sha256_bytes(source_bytes)}.py"
        elif frozen_spec.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE:
            if template_source is None or upload_source is not None:
                raise InvalidStrategySource("template versions require template_source only")
            source_bytes = self._validate_source_bytes(template_source, "template_source")
            upload_artifact = None
            template_artifact = f"templates/{_sha256_bytes(source_bytes)}.py"
        else:  # Defensive in case the enum grows without store policy.
            raise InvalidStrategySource(f"unsupported strategy source kind: {frozen_spec.source_kind}")

        source_hash = _sha256_bytes(source_bytes)
        version_hash = _version_hash(frozen_spec, source_hash, engine_version, compiler_version)
        spec_hash = canonical_sha256(frozen_spec)
        record = StrategyVersionRecord(
            version_id=version_hash,
            version_hash=version_hash,
            strategy_id=frozen_spec.strategy_id,
            source_kind=frozen_spec.source_kind,
            spec=frozen_spec,
            spec_hash=spec_hash,
            source_hash=source_hash,
            engine_version=engine_version,
            compiler_version=compiler_version,
            upload_artifact=upload_artifact,
            template_artifact=template_artifact if frozen_spec.source_kind is SourceKind.NATURAL_LANGUAGE_TEMPLATE else None,
        )
        record_bytes = canonical_json(record).encode("utf-8")

        versions_dir = self.root / "versions"
        record_path = versions_dir / f"{version_hash}.json"
        # Verify/reuse an existing immutable record before touching any source.
        if record_path.exists() or record_path.is_symlink():
            return self._read_record_path(record_path, expected_bytes=record_bytes)

        if upload_artifact is not None:
            source_path = self.root / upload_artifact
            self._write_immutable(source_path, source_bytes)
        elif record.template_artifact is not None:
            template_path = self.root / record.template_artifact
            self._write_immutable(template_path, source_bytes)
        self._write_immutable(record_path, record_bytes)
        return self.get_version(version_hash)

    def get_version(self, version_id: str) -> StrategyVersionRecord:
        """Load and fully verify a stored version and its uploaded source."""
        if not is_sha256(version_id):
            raise ValueError("version_id must be a lowercase SHA-256 hash")
        path = self.root / "versions" / f"{version_id}.json"
        record = self._read_record_path(path)
        if record.version_id != version_id:
            raise VersionStoreError("stored version id does not match its path")
        artifact = record.upload_artifact or record.template_artifact
        if artifact is not None:
            source_path = self.root / artifact
            if source_path.parent.is_symlink() or source_path.is_symlink() or not source_path.is_file():
                raise VersionStoreError("stored strategy source is missing or unsafe")
            if _sha256_bytes(source_path.read_bytes()) != record.source_hash:
                raise VersionStoreError("stored strategy source bytes do not match their hash")
        return record

    def read_upload_source(self, version_id: str) -> bytes:
        """Return byte-exact uploaded source for a Python version."""
        record = self.get_version(version_id)
        if record.source_kind is not SourceKind.PYTHON_UPLOAD or record.upload_artifact is None:
            raise InvalidStrategySource("strategy version does not contain uploaded Python source")
        return (self.root / record.upload_artifact).read_bytes()

    def read_template_source(self, version_id: str) -> bytes:
        """Return the exact trusted template source pinned by a version."""
        record = self.get_version(version_id)
        if record.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE or record.template_artifact is None:
            raise InvalidStrategySource("strategy version does not contain a trusted template source")
        return (self.root / record.template_artifact).read_bytes()

    def _validate_source_bytes(self, value: bytes, name: str) -> bytes:
        if type(value) is not bytes:
            raise InvalidStrategySource(f"{name} must be bytes")
        if not value:
            raise InvalidStrategySource(f"{name} must not be empty")
        if len(value) > self.max_source_bytes:
            raise InvalidStrategySource(
                f"{name} exceeds the {self.max_source_bytes}-byte source limit"
            )
        return value

    def _read_record_path(
        self,
        path: Path,
        *,
        expected_bytes: bytes | None = None,
    ) -> StrategyVersionRecord:
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise VersionStoreError(f"strategy version record not found or unsafe: {path.name}")
        raw = path.read_bytes()
        if expected_bytes is not None and raw != expected_bytes:
            raise ImmutableVersionConflict("existing strategy version record has different bytes")
        try:
            decoded = json.loads(raw)
            record = StrategyVersionRecord.model_validate_record(decoded)
        except VersionStoreError:
            raise
        except Exception as exc:
            raise VersionStoreError("stored strategy version record is invalid") from exc
        if canonical_json(record).encode("utf-8") != raw:
            raise VersionStoreError("stored strategy version record is not canonical")
        return record

    @staticmethod
    def _write_immutable(path: Path, content: bytes) -> None:
        """Atomically install bytes without replacing an existing path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise ImmutableVersionConflict(f"refusing to follow symlink at {path.name}")
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise ImmutableVersionConflict(f"immutable content conflict at {path.name}")
            return

        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Hard-link creation is atomic and fails if the destination
                # already exists; unlike replace(), it cannot mutate a record.
                os.link(temp_path, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                    raise ImmutableVersionConflict(f"immutable content conflict at {path.name}")
        finally:
            temp_path.unlink(missing_ok=True)
