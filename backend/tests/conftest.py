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
from sqlalchemy import text
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


# Tables that accumulate committed rows across tests. The migration baseline
# leaves every one of them empty, so they are truncated back to empty after each
# test. The seeded tables (`tenant`, `setting`) are restored separately below,
# and `alembic_version` is never touched.
_TRUNCATE_TABLES = (
    "admin_tenant",
    "app_user",
    "assignment_group",
    "assignment_group_member",
    "assignment_group_tenant",
    "audit_log",
    "auditor_tenant",
    "entra_user",
    "exclusion",
    "notification_log",
    "run",
    "user_session",
    "user_token",
)


@pytest_asyncio.fixture(autouse=True)
async def _restore_migration_baseline(migrated_engine) -> AsyncGenerator[None]:
    """Reset the database to the migration baseline after every test.

    The `session` fixture rolls back only the writes made through its own
    SAVEPOINT-wrapped connection. Many tests commit through a different path --
    the ASGI TestClient/httpx, `get_session_factory()`, a dedicated
    `create_async_engine`, or the multi-connection RLS helpers -- and those
    committed rows (most importantly the first admin/superadmin in `app_user`,
    but also settings, audit rows and test tenants) survive the rollback and
    leak into whatever test runs next. Under `pytest-randomly` that residue makes
    the "no admin/superadmin exists yet" setup tests fail order-dependently.

    This fixture runs on an owner connection from `migrated_engine`, which
    bypasses RLS, so it can clean every tenant's rows regardless of the RLS GUC.
    It is autouse and does not depend on `session`, so pytest sets it up before
    `session` and therefore tears it down after `session` has already rolled back
    and released its connection -- the cleanup runs on a free connection with no
    lock contention.
    """
    yield
    async with migrated_engine.begin() as conn:
        # Fail fast instead of hanging forever if a stray transaction still
        # holds a conflicting lock on one of the tables.
        await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        # Empty the non-seeded tables in one shot; CASCADE follows FKs between
        # them and RESTART IDENTITY keeps generated ids deterministic per test.
        await conn.execute(
            text("TRUNCATE TABLE " + ", ".join(_TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE")
        )
        # Seeded tables keep only their baseline rows. Deleting the non-default
        # tenants cascades to any settings/rows those tenants owned via FK.
        await conn.execute(text("DELETE FROM tenant WHERE NOT is_default"))
        # Keep the tenant id sequence deterministic: the next test-created tenant
        # gets the same id every run (the default tenant keeps its id=1).
        await conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('tenant', 'id'), "
                "(SELECT max(id) FROM tenant))"
            )
        )
        # Drop any test-created settings, then re-assert the one migration-seeded
        # instance setting at its baseline value (upsert so a test that deleted
        # it is healed too).
        await conn.execute(
            text(
                "DELETE FROM setting WHERE NOT (key = 'instance.multi_tenant_mode' "
                "AND tenant_id = (SELECT id FROM tenant WHERE is_default))"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO setting (key, value, is_secret, updated_at, tenant_id) "
                "VALUES ('instance.multi_tenant_mode', 'false'::jsonb, false, now(), "
                "(SELECT id FROM tenant WHERE is_default)) "
                "ON CONFLICT (tenant_id, key) "
                "DO UPDATE SET value = 'false'::jsonb, is_secret = false"
            )
        )
