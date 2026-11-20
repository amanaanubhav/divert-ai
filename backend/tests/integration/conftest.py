"""Integration fixtures: a throw-away database on a real PostgreSQL 16 + PostGIS 3.4.

    docker compose up -d postgres redis
    uv run pytest -m integration

DIVERT_TEST_DATABASE_URL (default below) names the test database; it is DROPPED and
re-created per session, migrated with Alembic (`upgrade head`) and seeded from the
committed OSM snapshots. Tests skip cleanly when the server is unreachable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from divert.cli import run_migrations, seed_all
from divert.config import Settings
from divert.db.seed import SeedReport
from divert.db.session import create_engine, create_sessionmaker
from divert.store.redis_store import create_redis

DEFAULT_URL = "postgresql+asyncpg://divert:divert@localhost:5432/divert_test"
DEFAULT_REDIS_URL = "redis://localhost:6379/15"


def _test_url() -> str:
    return os.environ.get("DIVERT_TEST_DATABASE_URL", DEFAULT_URL)


def _test_redis_url() -> str:
    return os.environ.get("DIVERT_TEST_REDIS_URL", DEFAULT_REDIS_URL)


async def recreate_database(url: str) -> None:
    u = make_url(url)
    admin = await asyncpg.connect(
        host=u.host, port=u.port or 5432, user=u.username, password=u.password,
        database="postgres", timeout=5,
    )  # fmt: skip
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{u.database}"')
    finally:
        await admin.close()


def database_url_for(name: str) -> str:
    return make_url(_test_url()).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_settings() -> Iterator[Settings]:
    return Settings(
        database_url=_test_url(),  # type: ignore[arg-type]
        redis_url=_test_redis_url(),  # type: ignore[arg-type]
        env="test",
        log_json=False,
        db_pool_size=5,
        metrics_port=0,
    )


@pytest.fixture(scope="session")
async def seeded_db(test_settings: Settings) -> list[SeedReport]:
    url = test_settings.sqlalchemy_url
    try:
        await recreate_database(url)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL not reachable for integration tests ({exc!r})")
    await asyncio.to_thread(run_migrations, url)
    return await seed_all(test_settings)


@pytest.fixture(scope="session")
async def engine(
    test_settings: Settings, seeded_db: list[SeedReport]
) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(test_settings, application_name="divert-tests")
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back (tests leave no trace)."""
    async with sessionmaker() as s:
        await s.begin()
        try:
            yield s
        finally:
            await s.rollback()


# ------------------------------------------------------------------ redis + history


@pytest.fixture
async def redis(test_settings: Settings) -> AsyncIterator[Redis]:
    """A clean Redis database per test (DIVERT_TEST_REDIS_URL, default db 15)."""
    client = create_redis(test_settings)
    try:
        await client.ping()
    except (RedisError, OSError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable for integration tests ({exc!r})")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def reset_history(sm: async_sessionmaker[AsyncSession]) -> None:
    """Forget every incident and free every unit (reference data is untouched)."""
    async with sm() as s, s.begin():
        await s.execute(
            sa.text(
                "UPDATE units SET status = 'AVAILABLE', current_dispatch_id = NULL "
                "WHERE status <> 'AVAILABLE' OR current_dispatch_id IS NOT NULL"
            )
        )
        await s.execute(sa.text("TRUNCATE outbox RESTART IDENTITY"))
        await s.execute(sa.text("DELETE FROM incidents"))  # cascades to dispatches & history


@pytest.fixture
async def clean_db(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await reset_history(sessionmaker)
    yield
    await reset_history(sessionmaker)
