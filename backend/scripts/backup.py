"""Create a consistent backup archive for the private SQLite deployment.

Run from the backend directory:

    uv run python scripts/backup.py --output ./backups/backup-20240101-120000.tar.gz

The archive holds ``manifest.json`` (format version, checksums, sizes and the
schema revision) plus a snapshot of the SQLite database taken through the
online backup API and every file below ``APP_STORAGE_DIR``. Use
``scripts/restore.py`` to put it back.

Only ``sqlite+aiosqlite:///`` database URLs are supported; anything else is
rejected instead of producing a misleading archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

APP_VERSION = "0.1.0"
BACKUP_FORMAT = "agent-platform-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "platform.db"
STORAGE_MEMBER = "files"
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/platform.db"
DEFAULT_STORAGE_DIR = "./data/files"
SQLITE_PREFIX = "sqlite+aiosqlite:///"
CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot proceed safely."""


def utc_timestamp() -> str:
    """Return a second-precision UTC timestamp in ISO-8601 form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sqlite_path_from_url(url: str) -> Path:
    """Resolve the on-disk database path from a SQLite SQLAlchemy URL.

    Uses the same ``split("///")`` convention as
    ``app/modules/runtime/checkpoint.py`` so backup and runtime agree on the
    file they are talking about.
    """
    if not url.startswith(SQLITE_PREFIX):
        raise BackupError(
            f"unsupported database URL {url!r}: backup and restore only support "
            f'SQLite deployments ("{SQLITE_PREFIX}<path>")'
        )
    raw_path = url.split("///", 1)[1]
    if not raw_path or raw_path == ":memory:":
        raise BackupError(
            f"database URL {url!r} has no on-disk file; in-memory databases cannot be backed up"
        )
    return Path(raw_path).expanduser().resolve()


def sha256_file(path: Path) -> str:
    """Return the hex SHA256 digest of ``path``."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _env_value(env: Mapping[str, str] | None, key: str, default: str) -> str:
    source = os.environ if env is None else env
    value = source.get(key)
    return default if value is None or not value.strip() else value


def _open_sqlite(database: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(database)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def snapshot_sqlite(database: Path, destination: Path) -> None:
    """Copy ``database`` with the SQLite online backup API.

    A raw ``cp`` of a live WAL database can capture a torn state; the backup
    API only ever writes a consistent snapshot.
    """
    if not database.is_file():
        raise BackupError(f"database file not found: {database}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_sqlite(database, destination, readonly=True)
    except sqlite3.OperationalError:
        # A read-only connection cannot recover a hot WAL in every setup;
        # fall back to a regular connection and keep the backup API guarantee.
        destination.unlink(missing_ok=True)
        _copy_sqlite(database, destination, readonly=False)
    os.chmod(destination, 0o600)


def _copy_sqlite(database: Path, destination: Path, *, readonly: bool) -> None:
    source = _open_sqlite(database, readonly=readonly)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def read_schema_revision(database: Path) -> str | None:
    """Return the Alembic revision recorded in the snapshot, if any."""
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def storage_files(storage_root: Path) -> list[Path]:
    """List regular files below the storage root, in a stable order."""
    if not storage_root.exists():
        raise BackupError(f"storage directory not found: {storage_root}")
    if not storage_root.is_dir():
        raise BackupError(f"storage path is not a directory: {storage_root}")
    files = [
        path
        for path in storage_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    return sorted(files, key=lambda path: path.relative_to(storage_root).as_posix())


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes, mode: int, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, BytesIO(payload))


def _add_file(tar: tarfile.TarFile, source: Path, arcname: str) -> tuple[int, str, int]:
    """Archive one regular file, returning the bytes actually written."""
    with source.open("rb") as handle:
        result = os.fstat(handle.fileno())
        size = result.st_size
        mode = stat.S_IMODE(result.st_mode) or 0o644
        info = tarfile.TarInfo(arcname)
        info.size = size
        info.mode = mode
        info.mtime = int(result.st_mtime)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, handle)
        handle.seek(0)
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return size, digest, mode


def create_backup(
    *,
    output: str | Path | None = None,
    database_url: str | None = None,
    storage_dir: str | Path | None = None,
    force: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create the archive atomically and return a JSON-serialisable summary."""
    url = database_url or _env_value(env, "APP_DATABASE_URL", DEFAULT_DATABASE_URL)
    database = sqlite_path_from_url(url)
    storage_root = Path(
        storage_dir or _env_value(env, "APP_STORAGE_DIR", DEFAULT_STORAGE_DIR)
    ).expanduser().resolve()

    created = datetime.now(UTC).replace(microsecond=0)
    target = (
        Path(output).expanduser()
        if output is not None
        else Path("backups") / f"backup-{created:%Y%m%d-%H%M%S}.tar.gz"
    )
    target = target.resolve()
    if target.exists() and not force:
        raise BackupError(f"refusing to overwrite existing archive {target} (pass --force)")
    target.parent.mkdir(parents=True, exist_ok=True)

    sources = storage_files(storage_root)
    created_at = utc_timestamp()
    created_epoch = int(created.timestamp())

    with tempfile.TemporaryDirectory(prefix="agent-platform-backup-") as workspace:
        snapshot = Path(workspace) / DATABASE_MEMBER
        snapshot_sqlite(database, snapshot)
        schema_revision = read_schema_revision(snapshot)

        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        try:
            entries: list[dict[str, Any]] = []
            with tarfile.open(temporary, "w:gz") as tar:
                db_size, db_digest, _ = _add_file(tar, snapshot, DATABASE_MEMBER)
                for source in sources:
                    relative = source.relative_to(storage_root).as_posix()
                    size, digest, mode = _add_file(tar, source, f"{STORAGE_MEMBER}/{relative}")
                    entries.append(
                        {"path": relative, "size": size, "sha256": digest, "mode": mode}
                    )
                manifest = {
                    "format": BACKUP_FORMAT,
                    "format_version": FORMAT_VERSION,
                    "app_version": APP_VERSION,
                    "created_at": created_at,
                    "database": {
                        "member": DATABASE_MEMBER,
                        "size": db_size,
                        "sha256": db_digest,
                        "schema_revision": schema_revision,
                    },
                    "storage": {
                        "member": STORAGE_MEMBER,
                        "file_count": len(entries),
                        "total_size": sum(entry["size"] for entry in entries),
                        "files": entries,
                    },
                }
                payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                _add_bytes(tar, MANIFEST_NAME, payload.encode("utf-8"), 0o600, created_epoch)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    return {
        "ok": True,
        "archive": str(target),
        "archive_size": target.stat().st_size,
        "format_version": FORMAT_VERSION,
        "app_version": APP_VERSION,
        "created_at": created_at,
        "database": {
            "path": str(database),
            "size": manifest["database"]["size"],
            "sha256": manifest["database"]["sha256"],
            "schema_revision": schema_revision,
        },
        "storage": {
            "dir": str(storage_root),
            "file_count": manifest["storage"]["file_count"],
            "total_size": manifest["storage"]["total_size"],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a consistent SQLite + local-storage backup archive.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="archive path (default: backups/backup-YYYYmmdd-HHMMSS.tar.gz)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override APP_DATABASE_URL; only sqlite+aiosqlite:/// is supported",
    )
    parser.add_argument(
        "--storage-dir",
        default=None,
        help="override APP_STORAGE_DIR",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the archive if it already exists",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_backup(
            output=args.output,
            database_url=args.database_url,
            storage_dir=args.storage_dir,
            force=args.force,
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
