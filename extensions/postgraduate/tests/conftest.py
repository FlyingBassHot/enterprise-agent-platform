"""Isolated platform database for extension integration tests; never use live data."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))
os.environ["APP_DATABASE_URL"] = (
    f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='postgrad-tests-')}/platform.db"
)
os.environ["APP_ENV"] = "test"
os.environ["APP_AUTO_CREATE_TABLES"] = "true"


@pytest.fixture
async def db(tmp_path):
    import app.infrastructure.database.registry  # noqa: F401
    from app.infrastructure.database.base import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/platform.db")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
            await session.rollback()
    finally:
        await engine.dispose()
