"""Consistent offline backup and fail-closed restore for execution SQLite.

The execution services share one SQLite database. Backups use SQLite's online
backup API, so committed WAL transactions are included. Restore always targets
a new file and rewrites any persisted execution gate to RECOVERING; broker
state must be reconciled again before activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


FORMAT_VERSION = 1
DATABASE_FILENAME = "execution.sqlite3"
MANIFEST_FILENAME = "manifest.json"


class ExecutionDatabaseBackupError(RuntimeError):
    """An execution database backup or restore failed validation."""


@dataclass(frozen=True, slots=True)
class ExecutionDatabaseBackupManifest:
    format_version: int
    created_at_utc: str
    database_sha256: str
    database_bytes: int
    user_version: int
    schema_sha256: str
    schema_object_count: int


@dataclass(frozen=True, slots=True)
class ExecutionDatabaseRestoreReceipt:
    backup_sha256: str
    restored_path: str
    recovered_control_rows: int
    requires_broker_reconciliation: bool = True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _schema_objects(connection: sqlite3.Connection) -> list[dict[str, str | None]]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','view','trigger') "
        "ORDER BY type,name"
    ).fetchall()
    return [
        {"type": str(row[0]), "name": str(row[1]), "table": str(row[2]), "sql": row[3]}
        for row in rows
    ]


def _schema_sha256(connection: sqlite3.Connection) -> tuple[str, int]:
    objects = _schema_objects(connection)
    encoded = json.dumps(objects, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(objects)


def _integrity_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise ExecutionDatabaseBackupError("SQLite integrity_check did not return exactly 'ok'")
    fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise ExecutionDatabaseBackupError("SQLite foreign_key_check found inconsistent rows")


def _read_manifest(backup_directory: Path) -> ExecutionDatabaseBackupManifest:
    manifest_path = backup_directory / MANIFEST_FILENAME
    database_path = backup_directory / DATABASE_FILENAME
    if backup_directory.is_symlink() or not backup_directory.is_dir():
        raise ExecutionDatabaseBackupError("backup must be a real directory")
    if manifest_path.is_symlink() or database_path.is_symlink():
        raise ExecutionDatabaseBackupError("backup files cannot be symlinks")
    if {entry.name for entry in backup_directory.iterdir()} != {MANIFEST_FILENAME, DATABASE_FILENAME}:
        raise ExecutionDatabaseBackupError("backup directory has missing or unexpected files")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExecutionDatabaseBackupError("backup manifest is unreadable or invalid") from exc
    fields = {
        "format_version", "created_at_utc", "database_sha256", "database_bytes",
        "user_version", "schema_sha256", "schema_object_count",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ExecutionDatabaseBackupError("backup manifest fields do not match the supported format")
    if (
        type(raw["format_version"]) is not int
        or raw["format_version"] != FORMAT_VERSION
        or not isinstance(raw["created_at_utc"], str)
        or not isinstance(raw["database_sha256"], str)
        or len(raw["database_sha256"]) != 64
        or any(ch not in "0123456789abcdef" for ch in raw["database_sha256"])
        or type(raw["database_bytes"]) is not int
        or raw["database_bytes"] <= 0
        or type(raw["user_version"]) is not int
        or raw["user_version"] < 0
        or not isinstance(raw["schema_sha256"], str)
        or len(raw["schema_sha256"]) != 64
        or any(ch not in "0123456789abcdef" for ch in raw["schema_sha256"])
        or type(raw["schema_object_count"]) is not int
        or raw["schema_object_count"] < 0
    ):
        raise ExecutionDatabaseBackupError("backup manifest values are malformed")
    try:
        timestamp = datetime.fromisoformat(raw["created_at_utc"])
    except ValueError as exc:
        raise ExecutionDatabaseBackupError("backup timestamp is malformed") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ExecutionDatabaseBackupError("backup timestamp must be timezone-aware")
    return ExecutionDatabaseBackupManifest(**raw)


def _validate_database(path: Path, manifest: ExecutionDatabaseBackupManifest) -> None:
    if not path.is_file() or path.is_symlink():
        raise ExecutionDatabaseBackupError("backup database is missing or not a regular file")
    if path.stat().st_size != manifest.database_bytes or _sha256_file(path) != manifest.database_sha256:
        raise ExecutionDatabaseBackupError("backup database size or SHA-256 does not match its manifest")
    try:
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            _integrity_check(connection)
            schema_hash, object_count = _schema_sha256(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise ExecutionDatabaseBackupError("backup database cannot be opened or checked") from exc
    if (
        schema_hash != manifest.schema_sha256
        or object_count != manifest.schema_object_count
        or user_version != manifest.user_version
    ):
        raise ExecutionDatabaseBackupError("backup database schema does not match its manifest")


def verify_execution_database_backup(backup_directory: str | Path) -> ExecutionDatabaseBackupManifest:
    """Verify the exact backup payload, schema fingerprint, and SQLite health."""
    directory = Path(backup_directory).expanduser().absolute()
    manifest = _read_manifest(directory)
    _validate_database(directory / DATABASE_FILENAME, manifest)
    return manifest


def create_execution_database_backup(
    source_database: str | Path,
    backup_directory: str | Path,
) -> ExecutionDatabaseBackupManifest:
    """Create an immutable new backup directory from one live SQLite database.

    The destination is exclusively created and never overwritten. The SQLite
    backup API captures a transactionally consistent view including committed
    WAL pages. A completed manifest is written last; incomplete directories
    are rejected by verification.
    """
    source = Path(source_database).expanduser().resolve(strict=True)
    target = Path(backup_directory).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise ExecutionDatabaseBackupError("source database must be a regular file")
    if not target.parent.is_dir():
        raise ExecutionDatabaseBackupError("backup parent directory must already exist")
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ExecutionDatabaseBackupError("backup destination already exists") from exc

    db_path = target / DATABASE_FILENAME
    try:
        source_uri = source.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_connection:
            with closing(sqlite3.connect(db_path, timeout=30)) as destination_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.01)
                # The source may persist `journal_mode=WAL` in its database
                # header. A backup directory is a portable artifact, so
                # checkpoint the destination and switch it back to DELETE
                # mode before hashing it; otherwise SQLite leaves `-wal` and
                # `-shm` files beside the supposedly self-contained image.
                mode = destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                if not mode or str(mode[0]).lower() != "delete":
                    raise ExecutionDatabaseBackupError("backup database could not be finalized without WAL sidecars")
                _integrity_check(destination_connection)
                schema_hash, object_count = _schema_sha256(destination_connection)
                user_version = int(destination_connection.execute("PRAGMA user_version").fetchone()[0])
        with db_path.open("rb") as database_file:
            os.fsync(database_file.fileno())
        manifest = ExecutionDatabaseBackupManifest(
            format_version=FORMAT_VERSION,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            database_sha256=_sha256_file(db_path),
            database_bytes=db_path.stat().st_size,
            user_version=user_version,
            schema_sha256=schema_hash,
            schema_object_count=object_count,
        )
        manifest_path = target / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with manifest_path.open("rb") as manifest_file:
            os.fsync(manifest_file.fileno())
        directory_fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return verify_execution_database_backup(target)
    except Exception:
        # The destination was atomically reserved above. Remove only files
        # SQLite itself may have created for this destination; never recurse
        # into or erase unrelated/unexpected content.
        for owned in (
            db_path,
            Path(str(db_path) + "-wal"),
            Path(str(db_path) + "-shm"),
            target / MANIFEST_FILENAME,
        ):
            try:
                owned.unlink()
            except FileNotFoundError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
        raise


def restore_execution_database_backup(
    backup_directory: str | Path,
    target_database: str | Path,
) -> ExecutionDatabaseRestoreReceipt:
    """Restore to a new database path and force stored execution gates closed.

    Existing targets are never overwritten. Any `execution_control` rows in
    the recovered image become RECOVERING so a caller must reacquire its owner
    lease and complete fresh broker reconciliation before dispatch.
    """
    backup = Path(backup_directory).expanduser().absolute()
    manifest = verify_execution_database_backup(backup)
    target = Path(target_database).expanduser().absolute()
    if not target.parent.is_dir():
        raise ExecutionDatabaseBackupError("restore parent directory must already exist")
    if target.exists() or target.is_symlink():
        raise ExecutionDatabaseBackupError("restore target already exists")

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    recovered_rows = 0
    try:
        with (backup / DATABASE_FILENAME).open("rb") as source_stream, temp_path.open("wb") as target_stream:
            while block := source_stream.read(1024 * 1024):
                target_stream.write(block)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        if _sha256_file(temp_path) != manifest.database_sha256:
            raise ExecutionDatabaseBackupError("copied restore payload does not match verified backup")
        with closing(sqlite3.connect(temp_path, isolation_level=None)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_control'"
            ).fetchone()
            if table:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(execution_control)")}
                required = {"state", "reason", "updated_at"}
                if not required.issubset(columns):
                    connection.rollback()
                    raise ExecutionDatabaseBackupError("execution_control table has an unsupported schema")
                cursor = connection.execute(
                    "UPDATE execution_control SET state='RECOVERING', "
                    "reason='backup_restore_requires_broker_reconciliation', updated_at=?",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                recovered_rows = cursor.rowcount
            run_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_run'"
            ).fetchone()
            if run_table:
                run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(execution_run)")}
                if not {"run_id", "state"}.issubset(run_columns):
                    connection.rollback()
                    raise ExecutionDatabaseBackupError("execution_run table has an unsupported schema")
                connection.execute(
                    "UPDATE execution_run SET state='PAUSED' WHERE state='RUNNING'"
                )
            connection.commit()
            _integrity_check(connection)

        # A hard link is an atomic no-replace install on the same filesystem.
        # If another process created the target since the initial check, link
        # fails rather than replacing it.
        os.link(temp_path, target)
        temp_path.unlink()
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return ExecutionDatabaseRestoreReceipt(
            backup_sha256=manifest.database_sha256,
            restored_path=str(target),
            recovered_control_rows=recovered_rows,
        )
    except FileExistsError as exc:
        raise ExecutionDatabaseBackupError("restore target already exists") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def apply_atomic_execution_database_migration(
    database_path: str | Path,
    backup_directory: str | Path,
    migration: Callable[[sqlite3.Connection], None],
) -> ExecutionDatabaseBackupManifest:
    """Back up, then run one SQLite-only migration in a rollback transaction.

    The application/runtime must be stopped before migration. The callback
    receives the sole connection and must not commit or close it. Any exception
    rolls the migration back; the pre-migration backup remains available for
    verified recovery.
    """
    if not callable(migration):
        raise TypeError("migration must be callable")
    manifest = create_execution_database_backup(database_path, backup_directory)
    path = Path(database_path).expanduser().resolve(strict=True)
    try:
        with closing(sqlite3.connect(path, isolation_level=None, timeout=30)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            migration(connection)
            if not connection.in_transaction:
                raise ExecutionDatabaseBackupError("migration callback ended its required rollback transaction")
            _integrity_check(connection)
            connection.commit()
    except Exception:
        try:
            with closing(sqlite3.connect(path, timeout=30)) as connection:
                _integrity_check(connection)
        except sqlite3.Error as validation_error:
            raise ExecutionDatabaseBackupError(
                "migration failed and the source database integrity could not be verified"
            ) from validation_error
        raise
    return manifest


__all__ = [
    "ExecutionDatabaseBackupError",
    "ExecutionDatabaseBackupManifest",
    "ExecutionDatabaseRestoreReceipt",
    "apply_atomic_execution_database_migration",
    "create_execution_database_backup",
    "restore_execution_database_backup",
    "verify_execution_database_backup",
]
