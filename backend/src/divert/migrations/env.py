"""Alembic environment (async, asyncpg). URL comes from DIVERT_DATABASE_URL unless the
caller already set ``sqlalchemy.url`` on the Config (tests, ``divert migrate``)."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from divert.config import Settings
from divert.db import models  # noqa: F401  (registers tables on Base.metadata)
from divert.db.base import Base
from divert.migrations.filters import include_object

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    return url if url else Settings().sqlalchemy_url


def _configure(connection: Connection | None = None, **kw: Any) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        transaction_per_migration=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run_sync)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
