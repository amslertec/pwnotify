"""Gemeinsame Test-Fixtures: echte Postgres-DB für Isolations-/RLS-Tests.

SQLite scheidet aus (kein RLS). Die Test-DB-URL kommt aus PWNOTIFY_TEST_DATABASE_URL;
Default zeigt auf eine lokale `pwnotify_test`-DB.
"""

from __future__ import annotations

import asyncio
import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_URL = os.environ.get(
    "PWNOTIFY_TEST_DATABASE_URL",
    # Lokaler Default: dedizierter Test-Container auf Host-Port 5433 (siehe Step 4).
    # In CI wird die Variable auf den Service-Container (localhost:5432) gesetzt.
    "postgresql+asyncpg://pwnotify:pwnotify@localhost:5433/pwnotify_test",
)


@pytest_asyncio.fixture(scope="session")
async def migrated_engine():
    # Alembic liest die DB-URL aus get_settings(); auf die Test-DB umbiegen und Cache leeren.
    from app.core.config import get_settings
    from app.db.migrate import run_migrations

    os.environ["PWNOTIFY_DATABASE_URL"] = TEST_DB_URL
    get_settings.cache_clear()
    # run_migrations() nutzt asyncio.run intern -> im Thread ausführen.
    await asyncio.to_thread(run_migrations)

    engine = create_async_engine(TEST_DB_URL, future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(migrated_engine) -> AsyncSession:
    factory = async_sessionmaker(migrated_engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
        await s.rollback()
