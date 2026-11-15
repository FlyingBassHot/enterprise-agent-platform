from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.infrastructure.database.registry  # noqa: E402,F401  (register all models)
from app.core.config import get_settings  # noqa: E402
from app.infrastructure.database.base import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
if settings.is_sqlite and "///" in settings.database_url:
    db_path = settings.database_url.split("///", 1)[1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
target_metadata = Base.metadata


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    kwargs = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "render_as_batch": (url or settings.database_url).startswith("sqlite"),
    }
    if connection is not None:
        context.configure(connection=connection, **kwargs)
    else:
        context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **kwargs)


def run_migrations_offline() -> None:
    _configure(url=settings.database_url)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
