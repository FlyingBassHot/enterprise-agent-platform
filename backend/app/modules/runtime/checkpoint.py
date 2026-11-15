"""LangGraph checkpoint persistence.

LangGraph owns the checkpoint schema; we only provide a dedicated SQLite
connection (separate from the SQLAlchemy pool) on the platform database file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import get_settings

_checkpointers: dict[int, AsyncSqliteSaver] = {}


def checkpoint_database_path() -> str:
    url = get_settings().database_url
    if "///" in url:
        path = url.split("///", 1)[1]
    else:  # pragma: no cover - non-sqlite deployments are not supported yet
        path = "./data/platform.db"
    if path.startswith(":memory:"):
        return path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


async def create_checkpointer(path: str | None = None) -> AsyncSqliteSaver:
    """Open an independent checkpointer connection (caller owns its lifecycle)."""
    db_path = path or checkpoint_database_path()
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA busy_timeout=30000")
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver


async def get_checkpointer() -> AsyncSqliteSaver:
    """Lazy per-event-loop singleton used by the application runtime."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    saver = _checkpointers.get(key)
    if saver is None:
        saver = await create_checkpointer()
        _checkpointers[key] = saver
    return saver


async def close_checkpointer(saver: AsyncSqliteSaver | None = None) -> None:
    if saver is None:
        loop = asyncio.get_running_loop()
        saver = _checkpointers.pop(id(loop), None)
        if saver is None:
            return
    else:
        for key, value in list(_checkpointers.items()):
            if value is saver:
                _checkpointers.pop(key, None)
    try:
        await saver.conn.close()
    except Exception:  # noqa: BLE001 - closing a dead connection must not raise
        pass


__all__ = [
    "checkpoint_database_path",
    "close_checkpointer",
    "create_checkpointer",
    "get_checkpointer",
]
