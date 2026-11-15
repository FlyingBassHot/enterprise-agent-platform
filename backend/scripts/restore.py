"""Restore a backup produced by ``scripts/backup.py``.

Run from the backend directory:

    uv run python scripts/restore.py --archive ./backups/backup-20240101-120000.tar.gz \
        --data-dir ./data

The archive is opened read-only and every byte is checked against the manifest
before anything below ``--data-dir`` is replaced. Extraction happens in a
staging directory; the database is moved into place atomically and a
``.restore-incomplete`` marker stays behind until the whole restore succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup import (  # noqa: E402
    BACKUP_FORMAT,
    CHUNK_SIZE,
    DATABASE_MEMBER,
    FORMAT_VERSION,
    MANIFEST_NAME,
    STORAGE_MEMBER,
    BackupError,
    sqlite_path_from_url,
    utc_timestamp,
)

MARKER_NAME = ".restore-incomplete"
_SENSITIVE_HINTS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "api_key",
    "apikey",
    "private_key",
)
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".env")
_HEX_DIGITS = 64


class RestoreError(BackupError):
    """Raised when an archive is invalid or unsafe to restore."""


def _safe_member_name(name: str) -> str:
    """Normalise an archive member name, rejecting traversal and absolutes."""
    if not name or "\\" in name or name.startswith("/"):
        raise RestoreError(f"unsafe archive entry: {name!r}")
    parts = [part for part in PurePosixPath(name).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise RestoreError(f"path traversal in archive entry: {name!r}")
    return "/".join(parts)


def _safe_storage_path(name: object) -> str:
    if not isinstance(name, str):
        raise RestoreError("manifest storage entry has a non-string path")
    normalised = _safe_member_name(name)
    if normalised != name or normalised == MANIFEST_NAME or normalised == DATABASE_MEMBER:
        raise RestoreError(f"invalid storage path in manifest: {name!r}")
    return normalised


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_DIGITS
        and all(character in "0123456789abcdef" for character in value)
    )


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RestoreError(f"manifest field {label} must be a non-negative integer")
    return value


def validate_manifest(data: object) -> dict[str, Any]:
    """Validate structure and return the manifest as a typed dict."""
    if not isinstance(data, dict):
        raise RestoreError("manifest.json must contain a JSON object")
    if data.get("format") != BACKUP_FORMAT:
        raise RestoreError(f"unexpected backup format: {data.get('format')!r}")
    version = data.get("format_version")
    if not isinstance(version, int) or version < 1 or version > FORMAT_VERSION:
        raise RestoreError(f"unsupported backup format version: {version!r}")
    if not isinstance(data.get("app_version"), str) or not data.get("app_version"):
        raise RestoreError("manifest is missing app_version")
    if not isinstance(data.get("created_at"), str) or not data.get("created_at"):
        raise RestoreError("manifest is missing created_at")

    database = data.get("database")
    if not isinstance(database, dict):
        raise RestoreError("manifest is missing the database section")
    if database.get("member") != DATABASE_MEMBER:
        raise RestoreError(f"unsupported database member: {database.get('member')!r}")
    _as_int(database.get("size"), "database.size")
    if not _is_sha256(database.get("sha256")):
        raise RestoreError("manifest has an invalid database checksum")
    revision = database.get("schema_revision")
    if revision is not None and not isinstance(revision, str):
        raise RestoreError("manifest has an invalid schema_revision")

    storage = data.get("storage")
    if not isinstance(storage, dict):
        raise RestoreError("manifest is missing the storage section")
    if storage.get("member") != STORAGE_MEMBER:
        raise RestoreError(f"unsupported storage member: {storage.get('member')!r}")
    files = storage.get("files")
    if not isinstance(files, list):
        raise RestoreError("manifest storage.files must be a list")
    _as_int(storage.get("file_count"), "storage.file_count")
    total_size = _as_int(storage.get("total_size"), "storage.total_size")
    if storage["file_count"] != len(files):
        raise RestoreError("manifest storage.file_count does not match its file list")
    seen: set[str] = set()
    computed_total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise RestoreError("manifest storage entry must be an object")
        path = _safe_storage_path(entry.get("path"))
        if path in seen:
            raise RestoreError(f"duplicate storage path in manifest: {path!r}")
        seen.add(path)
        computed_total += _as_int(entry.get("size"), f"storage.files[{path}].size")
        if not _is_sha256(entry.get("sha256")):
            raise RestoreError(f"manifest has an invalid checksum for {path!r}")
        if "mode" in entry:
            _as_int(entry["mode"], f"storage.files[{path}].mode")
    if computed_total != total_size:
        raise RestoreError("manifest storage.total_size does not match its file list")
    return data


def read_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError as exc:
        raise RestoreError(f"archive does not contain {MANIFEST_NAME}") from exc
    if not member.isfile():
        raise RestoreError(f"{MANIFEST_NAME} is not a regular file in the archive")
    handle = tar.extractfile(member)
    if handle is None:
        raise RestoreError(f"cannot read {MANIFEST_NAME} from the archive")
    with handle:
        raw = handle.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RestoreError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    return validate_manifest(data)


def _expected_members(manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Map archive member name -> (size, sha256) that must be present."""
    database = manifest["database"]
    expected = {DATABASE_MEMBER: (database["size"], database["sha256"])}
    for entry in manifest["storage"]["files"]:
        expected[f"{STORAGE_MEMBER}/{entry['path']}"] = (entry["size"], entry["sha256"])
    return expected


def validate_members(tar: tarfile.TarFile, manifest: dict[str, Any]) -> None:
    """Reject traversal, duplicates, unexpected entries and missing entries."""
    expected = _expected_members(manifest)
    seen: set[str] = set()
    for member in tar.getmembers():
        name = _safe_member_name(member.name)
        if name in seen:
            raise RestoreError(f"duplicate archive entry: {name!r}")
        seen.add(name)
        if not member.isfile():
            raise RestoreError(f"unsupported archive entry type: {name!r}")
        if name != MANIFEST_NAME and name not in expected:
            raise RestoreError(f"unexpected archive entry: {name!r}")
    missing = sorted(set(expected) - seen)
    if missing:
        raise RestoreError(f"archive is missing expected entries: {', '.join(missing)}")
    if MANIFEST_NAME not in seen:
        raise RestoreError(f"archive is missing {MANIFEST_NAME}")


def _extract_verified(
    tar: tarfile.TarFile,
    member_name: str,
    destination: Path,
    expected_size: int,
    expected_digest: str,
) -> None:
    member = tar.getmember(member_name)
    if member.size != expected_size:
        raise RestoreError(
            f"size mismatch for {member_name!r}: archive has {member.size}, "
            f"manifest expects {expected_size}"
        )
    source = tar.extractfile(member)
    if source is None:
        raise RestoreError(f"cannot read {member_name!r} from the archive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    with source, destination.open("wb") as sink:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            written += len(chunk)
            digest.update(chunk)
            sink.write(chunk)
    os.chmod(destination, 0o600)
    if written != expected_size:
        raise RestoreError(
            f"size mismatch for {member_name!r}: read {written}, expected {expected_size}"
        )
    if digest.hexdigest() != expected_digest:
        raise RestoreError(
            f"checksum mismatch for {member_name!r}: archive is corrupted or tampered with"
        )


def extract_verified(tar: tarfile.TarFile, manifest: dict[str, Any], staging: Path) -> None:
    """Extract every expected file into ``staging`` after verifying its digest."""
    database = manifest["database"]
    _extract_verified(
        tar,
        DATABASE_MEMBER,
        staging / DATABASE_MEMBER,
        database["size"],
        database["sha256"],
    )
    for entry in manifest["storage"]["files"]:
        relative = PurePosixPath(entry["path"])
        _extract_verified(
            tar,
            f"{STORAGE_MEMBER}/{entry['path']}",
            staging.joinpath(STORAGE_MEMBER, *relative.parts),
            entry["size"],
            entry["sha256"],
        )


def _effective_mode(path: str, entry: dict[str, Any]) -> int:
    name = PurePosixPath(path).name.lower()
    mode = int(entry.get("mode") or 0o644) & 0o777
    if not mode:
        mode = 0o644
    if name.endswith(_SENSITIVE_SUFFIXES) or any(hint in name for hint in _SENSITIVE_HINTS):
        return 0o600
    return mode


def _replace_file(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_database(source: Path, destination: Path) -> None:
    _replace_file(source, destination, 0o600)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)


def restore_archive(
    *,
    archive: str | Path,
    data_dir: str | Path,
    force: bool = False,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Verify and restore ``archive`` into ``data_dir`` atomically."""
    requested_archive = Path(archive).expanduser()
    if requested_archive.is_symlink():
        raise RestoreError(f"archive must not be a symbolic link: {requested_archive}")
    archive_path = requested_archive.resolve()
    if not archive_path.is_file():
        raise RestoreError(f"archive not found: {archive_path}")

    data_root = Path(data_dir).expanduser().resolve()
    database_destination = (
        sqlite_path_from_url(database_url) if database_url else data_root / DATABASE_MEMBER
    )
    storage_root = data_root / STORAGE_MEMBER

    if data_root.exists() and not data_root.is_dir():
        raise RestoreError(f"target data dir is not a directory: {data_root}")
    if data_root.is_dir() and any(data_root.iterdir()) and not force:
        raise RestoreError(f"target data dir is not empty: {data_root} (pass --force)")
    data_root.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=".restore-staging-", dir=data_root.parent))
    marker = data_root / MARKER_NAME
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            manifest = read_manifest(tar)
            validate_members(tar, manifest)
            data_root.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {"archive": str(archive_path), "started_at": utc_timestamp()},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)
            extract_verified(tar, manifest, staging)

        _install_database(staging / DATABASE_MEMBER, database_destination)
        restored_files = 0
        restored_bytes = 0
        storage_root.mkdir(parents=True, exist_ok=True)
        for entry in manifest["storage"]["files"]:
            relative = PurePosixPath(entry["path"])
            source = staging.joinpath(STORAGE_MEMBER, *relative.parts)
            destination = storage_root.joinpath(*relative.parts)
            _replace_file(source, destination, _effective_mode(entry["path"], entry))
            restored_files += 1
            restored_bytes += int(entry["size"])
        marker.unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "ok": True,
        "archive": str(archive_path),
        "data_dir": str(data_root),
        "database": str(database_destination),
        "database_size": manifest["database"]["size"],
        "schema_revision": manifest["database"].get("schema_revision"),
        "format_version": manifest["format_version"],
        "app_version": manifest["app_version"],
        "created_at": manifest["created_at"],
        "files_restored": restored_files,
        "files_bytes": restored_bytes,
        "forced": force,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore a SQLite + local-storage backup archive.",
    )
    parser.add_argument("--archive", required=True, help="backup archive to restore")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="target data dir; the database goes to <data-dir>/platform.db and "
        "storage files to <data-dir>/files",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override the restore destination database URL "
        "(default: sqlite+aiosqlite:///<data-dir>/platform.db)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="restore into a non-empty data dir, replacing existing files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = restore_archive(
            archive=args.archive,
            data_dir=args.data_dir,
            force=args.force,
            database_url=args.database_url,
        )
    except (BackupError, OSError, tarfile.TarError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
