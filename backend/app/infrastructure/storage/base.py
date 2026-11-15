from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    key: str
    uri: str
    size: int
    content_type: str


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    def open(self, key: str) -> BinaryIO: ...

    async def delete(self, key: str) -> None: ...
