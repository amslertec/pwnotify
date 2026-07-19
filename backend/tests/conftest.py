"""Gemeinsame Test-Fixtures: echte Postgres-DB für Isolations-/RLS-Tests.

SQLite scheidet aus (kein RLS). Die Test-DB-URL kommt aus PWNOTIFY_TEST_DATABASE_URL;
Default zeigt auf eine lokale `pwnotify_test`-DB.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Tests dürfen NICHT vom Deployment-Pfad `/data` abhängen: GitHub-CI-Runner laufen als
# non-root und können `/data` nicht anlegen, sodass `crypto._load_key` (secret.key-Erzeugung)
# und die Avatar-Schreibpfade sonst mit `PermissionError: '/data'` fehlschlagen. Ein
# schreibbares Temp-`data_dir` plus ein fester Fernet-Testschlüssel (KEIN echtes Secret) machen
# die Suite unabhängig von `/data`. `setdefault`: ein aussen gesetzter Wert gewinnt. Muss VOR
# dem ersten `get_settings()`-Aufruf stehen -- Conftest wird von pytest zuerst importiert.
os.environ.setdefault("PWNOTIFY_DATA_DIR", tempfile.mkdtemp(prefix="pwnotify-test-data-"))
os.environ.setdefault("PWNOTIFY_SECRET_KEY", "ZLh5EzxtulsRYjWpEqW8Ax1lL2P40JugKp2DjUpGsUU=")

# Password for the `pwnotify_runtime` login role (see `app/db/session.py::get_runtime_engine`).
# `setdefault`: an externally set value (CI) wins. Must be stable across runs -- the role's
# password is re-set idempotently by the provisioning migration every time migrations run
# against the test DB, so the runtime engine (derived from this same env var) always matches.
os.environ.setdefault("PWNOTIFY_RUNTIME_DB_PASSWORD", "runtime-test-pw")

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
    from app.db import session as db_session
    from app.db.migrate import run_migrations

    prev_url = os.environ.get("PWNOTIFY_DATABASE_URL")
    os.environ["PWNOTIFY_DATABASE_URL"] = TEST_DB_URL
    get_settings.cache_clear()
    await db_session.dispose_engine()

    # run_migrations() nutzt asyncio.run intern -> im Thread ausführen.
    await asyncio.to_thread(run_migrations)

    engine = create_async_engine(TEST_DB_URL, future=True)
    yield engine
    await engine.dispose()
    await db_session.dispose_engine()

    if prev_url is None:
        os.environ.pop("PWNOTIFY_DATABASE_URL", None)
    else:
        os.environ["PWNOTIFY_DATABASE_URL"] = prev_url
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def session(migrated_engine) -> AsyncGenerator[AsyncSession]:
    async with migrated_engine.connect() as conn:
        outer = await conn.begin()
        factory = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            class_=AsyncSession,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as s:
            yield s
        await outer.rollback()
