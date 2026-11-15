from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.storage.base import StoredObject


class LocalFileStorage:
    """Filesystem-backed object storage; key paths are server-generated."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_settings().storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root)):
            raise ValueError("invalid storage key")
        return path

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return StoredObject(key=key, uri=path.as_uri(), size=len(data), content_type=content_type)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return await asyncio.to_thread(path.read_bytes)

    def open(self, key: str):
        return self._path(key).open("rb")

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)


_storage: LocalFileStorage | None = None


def get_storage() -> LocalFileStorage:
    global _storage
    if _storage is None:
        _storage = LocalFileStorage()
    return _storage
