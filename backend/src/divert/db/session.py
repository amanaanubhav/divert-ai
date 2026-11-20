"""Async engine/session factory (asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from divert.config import Settings


def create_engine(settings: Settings, *, application_name: str = "divert") -> AsyncEngine:
    server_settings = {"application_name": application_name, "timezone": "UTC"}
    if settings.db_statement_timeout_ms:
        server_settings["statement_timeout"] = str(settings.db_statement_timeout_ms)
    return create_async_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=settings.db_echo,
        connect_args={"server_settings": server_settings},
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """``async with transaction(sm) as s:`` - commits on success, rolls back on error."""
    async with factory() as session, session.begin():
        yield session
