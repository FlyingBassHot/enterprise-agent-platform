from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import stat
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import scripts.backup as backup_cli
import scripts.restore as restore_cli
from scripts.backup import (
    APP_VERSION,
    DATABASE_MEMBER,
    FORMAT_VERSION,
    MANIFEST_NAME,
    STORAGE_MEMBER,
    BackupError,
    create_backup,
)
from scripts.restore import MARKER_NAME, RestoreError, restore_archive

ORIGINAL_ROWS = ["alpha", "beta", "gamma"]
STORAGE_CONTENT = {
    "report.txt": b"quarterly report\n",
    "nested/payload.bin": bytes(range(256)) * 3,
}


@pytest.fixture
def deployment(tmp_path: Path):
    """A live deployment: WAL SQLite database plus local storage files."""
    data_dir = tmp_path / "deployment"
    storage = data_dir / "files"
    (storage / "nested").mkdir(parents=True)
    (storage / "report.txt").write_bytes(STORAGE_CONTENT["report.txt"])
    (storage / "nested" / "payload.bin").write_bytes(STORAGE_CONTENT["nested/payload.bin"])
    password_file = storage / "password.txt"
    password_file.write_bytes(b"server-password\n")
    password_file.chmod(0o644)

    database = data_dir / "platform.db"
    seed = sqlite3.connect(database)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    seed.execute("INSERT INTO alembic_version VALUES ('0001_initial')")
    seed.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    seed.executemany("INSERT INTO items (name) VALUES (?)", [("alpha",), ("beta",)])
    seed.commit()
    # A second open connection keeps the WAL hot while the backup runs.
    writer = sqlite3.connect(database)
    writer.execute("INSERT INTO items (name) VALUES ('gamma')")
    writer.commit()
    try:
        yield {
            "data_dir": data_dir,
            "database": database,
            "storage": storage,
            "database_url": f"sqlite+aiosqlite:///{database}",
            "writer": writer,
        }
    finally:
        writer.close()
        seed.close()


@pytest.fixture
def backup_archive(deployment, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    archive = tmp_path / "backup.tar.gz"
    summary = create_backup(
        output=archive,
        database_url=deployment["database_url"],
        storage_dir=deployment["storage"],
    )
    return archive, summary


def _read_members(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive, "r:gz") as tar:
        return {
            member.name: payload
            for member in tar.getmembers()
            if member.isfile()
            for payload in [tar.extractfile(member).read()]
        }


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    replace: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
) -> None:
    replacements = replace or {}
    with tarfile.open(source, "r:gz") as tar:
        members = [
            (copy.copy(member), tar.extractfile(member).read())
            for member in tar.getmembers()
            if member.isfile()
        ]
    with tarfile.open(destination, "w:gz") as tar:
        for info, payload in members:
            data = replacements.get(info.name, payload)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
        for name, payload in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, BytesIO(payload))


def _tamper(payload: bytes) -> bytes:
    return payload[:-1] + bytes([payload[-1] ^ 0xFF])


def _rows(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return [row[0] for row in connection.execute("SELECT name FROM items ORDER BY id")]
    finally:
        connection.close()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backup_manifest_has_expected_fields(deployment, backup_archive):
    archive, summary = backup_archive
    assert summary["ok"] is True
    assert archive.is_file()
    assert _mode(archive) == 0o600
    assert summary["archive_size"] == archive.stat().st_size

    members = _read_members(archive)
    assert MANIFEST_NAME in members
    manifest = json.loads(members[MANIFEST_NAME])
    assert manifest["format"] == "agent-platform-backup"
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["app_version"] == APP_VERSION == "0.1.0"
    assert manifest["created_at"].endswith("Z")

    database = manifest["database"]
    assert database["member"] == DATABASE_MEMBER
    assert database["schema_revision"] == "0001_initial"
    assert database["size"] > 0
    assert database["sha256"] == hashlib.sha256(members[DATABASE_MEMBER]).hexdigest()

    storage = manifest["storage"]
    expected = {**STORAGE_CONTENT, "password.txt": b"server-password\n"}
    assert storage["member"] == STORAGE_MEMBER
    assert storage["file_count"] == len(expected)
    assert storage["total_size"] == sum(len(data) for data in expected.values())
    entries = {entry["path"]: entry for entry in storage["files"]}
    assert set(entries) == set(expected)
    for path, data in expected.items():
        assert entries[path]["size"] == len(data)
        assert entries[path]["sha256"] == hashlib.sha256(data).hexdigest()
        assert isinstance(entries[path]["mode"], int)
        assert members[f"{STORAGE_MEMBER}/{path}"] == data

    snapshot = members[DATABASE_MEMBER]
    snapshot_path = deployment["data_dir"].parent / "snapshot.db"
    snapshot_path.write_bytes(snapshot)
    assert _rows(snapshot_path) == ORIGINAL_ROWS


def test_backup_refuses_overwrite_without_force(deployment, backup_archive):
    archive, _ = backup_archive
    with pytest.raises(BackupError, match="--force"):
        create_backup(
            output=archive,
            database_url=deployment["database_url"],
            storage_dir=deployment["storage"],
        )
    summary = create_backup(
        output=archive,
        database_url=deployment["database_url"],
        storage_dir=deployment["storage"],
        force=True,
    )
    assert summary["ok"] is True


def test_backup_rejects_unsupported_database_url(tmp_path: Path):
    with pytest.raises(BackupError, match="sqlite"):
        create_backup(
            output=tmp_path / "backup.tar.gz",
            database_url="postgresql+asyncpg://user:pass@localhost/platform",
            storage_dir=tmp_path,
        )
    with pytest.raises(BackupError, match="in-memory"):
        create_backup(
            output=tmp_path / "backup.tar.gz",
            database_url="sqlite+aiosqlite:///:memory:",
            storage_dir=tmp_path,
        )


def test_restore_roundtrip(deployment, backup_archive, tmp_path: Path):
    archive, _ = backup_archive
    # Mutate the live deployment after the backup was taken.
    deployment["writer"].execute("DELETE FROM items")
    deployment["writer"].commit()
    (deployment["storage"] / "report.txt").write_bytes(b"mutated\n")
    (deployment["storage"] / "nested" / "payload.bin").unlink()

    target = tmp_path / "restored"
    summary = restore_archive(archive=archive, data_dir=target)

    assert summary["ok"] is True
    assert summary["data_dir"] == str(target)
    assert summary["database"] == str(target / DATABASE_MEMBER)
    assert summary["schema_revision"] == "0001_initial"
    assert summary["files_restored"] == 3
    assert summary["forced"] is False

    assert _rows(target / DATABASE_MEMBER) == ORIGINAL_ROWS
    restored = _files(target / "files")
    assert restored["report.txt"] == STORAGE_CONTENT["report.txt"]
    assert restored["nested/payload.bin"] == STORAGE_CONTENT["nested/payload.bin"]
    assert restored["password.txt"] == b"server-password\n"

    assert _mode(target / DATABASE_MEMBER) == 0o600
    assert _mode(target / "files" / "password.txt") == 0o600
    assert _mode(target / "files" / "report.txt") == 0o644
    assert not (target / MARKER_NAME).exists()
    assert not (target / f"{DATABASE_MEMBER}-wal").exists()
    assert not (target / f"{DATABASE_MEMBER}-shm").exists()


def test_restore_rejects_tampered_checksums(backup_archive, tmp_path: Path):
    archive, _ = backup_archive
    members = _read_members(archive)
    original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    tampered_db = tmp_path / "tampered-db.tar.gz"
    _rewrite_archive(
        archive, tampered_db, replace={DATABASE_MEMBER: _tamper(members[DATABASE_MEMBER])}
    )
    with pytest.raises(RestoreError, match="checksum mismatch"):
        restore_archive(archive=tampered_db, data_dir=tmp_path / "db-target")
    assert not (tmp_path / "db-target" / DATABASE_MEMBER).exists()

    tampered_file = tmp_path / "tampered-file.tar.gz"
    _rewrite_archive(
        archive,
        tampered_file,
        replace={"files/report.txt": _tamper(members["files/report.txt"])},
    )
    with pytest.raises(RestoreError, match="checksum mismatch"):
        restore_archive(archive=tampered_file, data_dir=tmp_path / "file-target")
    assert not (tmp_path / "file-target" / DATABASE_MEMBER).exists()

    # The source archive must never be modified by a restore attempt.
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_digest


@pytest.mark.parametrize(
    "entry", ["files/../../evil.txt", "/tmp/evil.txt", "files/..\\evil.txt"]
)
def test_restore_rejects_path_traversal(backup_archive, tmp_path: Path, entry: str):
    archive, _ = backup_archive
    hostile = tmp_path / "hostile.tar.gz"
    _rewrite_archive(archive, hostile, extra={entry: b"pwned"})

    with pytest.raises(RestoreError, match="(traversal|unsafe)"):
        restore_archive(archive=hostile, data_dir=tmp_path / "target")

    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "target" / DATABASE_MEMBER).exists()


def test_restore_rejects_missing_manifest(backup_archive, tmp_path: Path):
    archive, _ = backup_archive
    assert MANIFEST_NAME in _read_members(archive)
    without_manifest = tmp_path / "without-manifest.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(without_manifest, "w:gz") as sink:
        for member in source.getmembers():
            if member.name == MANIFEST_NAME:
                continue
            payload = source.extractfile(member).read()
            sink.addfile(member, BytesIO(payload))
    with pytest.raises(RestoreError, match="manifest.json"):
        restore_archive(archive=without_manifest, data_dir=tmp_path / "target")


def test_restore_protects_non_empty_target(deployment, backup_archive, tmp_path: Path):
    archive, _ = backup_archive
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("keep me")

    with pytest.raises(RestoreError, match="not empty"):
        restore_archive(archive=archive, data_dir=target)
    assert (target / "keep.txt").read_text() == "keep me"
    assert not (target / DATABASE_MEMBER).exists()

    summary = restore_archive(archive=archive, data_dir=target, force=True)
    assert summary["ok"] is True
    assert summary["forced"] is True
    assert _rows(target / DATABASE_MEMBER) == ORIGINAL_ROWS
    assert (target / "keep.txt").read_text() == "keep me"


def test_restore_database_url_override(deployment, backup_archive, tmp_path: Path):
    archive, _ = backup_archive
    target = tmp_path / "override-target"
    database = tmp_path / "custom" / "custom.db"
    summary = restore_archive(
        archive=archive,
        data_dir=target,
        database_url=f"sqlite+aiosqlite:///{database}",
    )
    assert summary["database"] == str(database)
    assert _rows(database) == ORIGINAL_ROWS
    assert _mode(database) == 0o600
    assert not (target / DATABASE_MEMBER).exists()
    assert _files(target / "files")["report.txt"] == STORAGE_CONTENT["report.txt"]

    with pytest.raises(BackupError, match="sqlite"):
        restore_archive(
            archive=archive,
            data_dir=tmp_path / "other",
            database_url="postgresql+asyncpg://localhost/platform",
        )


def test_cli_help_and_json_summary(monkeypatch, deployment, tmp_path: Path, capsys):
    for module in (backup_cli, restore_cli):
        with pytest.raises(SystemExit) as exit_info:
            module.main(["--help"])
        assert exit_info.value.code == 0
    capsys.readouterr()

    monkeypatch.setenv("APP_DATABASE_URL", deployment["database_url"])
    monkeypatch.setenv("APP_STORAGE_DIR", str(deployment["storage"]))
    archive = tmp_path / "cli-backup.tar.gz"
    assert backup_cli.main(["--output", str(archive)]) == 0
    backup_summary = json.loads(capsys.readouterr().out)
    assert backup_summary["ok"] is True
    assert backup_summary["archive"] == str(archive)

    target = tmp_path / "cli-restored"
    assert restore_cli.main(["--archive", str(archive), "--data-dir", str(target)]) == 0
    restore_summary = json.loads(capsys.readouterr().out)
    assert restore_summary["ok"] is True
    assert _rows(target / DATABASE_MEMBER) == ORIGINAL_ROWS

    failure = tmp_path / "unwritable.tar.gz"
    exit_code = backup_cli.main(
        ["--output", str(failure), "--database-url", "postgresql+asyncpg://localhost/db"]
    )
    assert exit_code == 1
    assert json.loads(capsys.readouterr().err)["ok"] is False
