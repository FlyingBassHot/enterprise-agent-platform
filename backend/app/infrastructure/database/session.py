from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.database.base import Base

logger = get_logger(__name__)

_settings = get_settings()
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _make_engine() -> AsyncEngine:
    settings = get_settings()
    kwargs: dict = {"echo": settings.sql_echo, "pool_pre_ping": True}
    if settings.is_sqlite:
        if ":memory:" not in settings.database_url:
            db_path = settings.database_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_async_engine(settings.database_url, **kwargs)
    if settings.is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    settings = get_settings()
    if not settings.auto_create_tables:
        return
    import app.infrastructure.database.registry  # noqa: F401  (imports all models)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database schema ensured")


async def check_database() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("database health check failed")
        return False
