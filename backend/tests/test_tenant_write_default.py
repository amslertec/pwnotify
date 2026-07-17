"""Tests für den kontext-abhängigen ``tenant_id``-Default (Phase 3, Task 1).

Ersetzt die Phase-1-Brücke (statischer DB-``server_default`` = Default-Tenant-id): ein ORM-
INSERT innerhalb eines aktiven Tenant-Kontexts (``tenant_scoped_session``) stempelt
``tenant_id`` jetzt aus dem aktiven Tenant (ContextVar); ohne aktiven Kontext (Owner-Pfad,
kein Rollenwechsel) gibt es keinen Fallback mehr -- ein INSERT ohne explizit gesetztes
``tenant_id`` verletzt NOT NULL. Das ist der neue, gewollte Vertrag: Owner-Pfade müssen
``tenant_id`` künftig explizit setzen (Task 3/4).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.db import session as db_session
from app.db.tenant_context import tenant_scoped_session
from app.repositories import run_repo
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def temp_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Wegwerf-Tenant über eine eigene, echt committete Verbindung (wie test_isolation_attack.py):
    ``tenant_scoped_session`` öffnet eine eigene Verbindung auf der App-Engine und sieht daher
    keine uncommitteten Daten aus der savepoint-isolierten ``session``-Fixture."""
    async with migrated_engine.connect() as conn:
        tid = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('Write-Default-Test', 'write-default-test', true, now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.commit()
        try:
            yield tid
        finally:
            # ON DELETE CASCADE räumt abhängige run/exclusion/... Zeilen mit auf.
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tid})
            await conn.commit()


async def test_insert_in_tenant_context_stamps_active_tenant(temp_tenant: int) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        run = await run_repo.create(s, trigger="manual", dry_run=False)
    assert run.tenant_id == temp_tenant


async def test_owner_context_insert_without_tenant_id_raises(
    migrated_engine: AsyncEngine,
) -> None:
    """Ohne aktiven Tenant-Kontext (ContextVar unset, Owner-Rolle) gibt es keinen server_default
    mehr, der einspringt -- der INSERT muss mit einer NOT-NULL-Verletzung scheitern."""
    async for s in db_session.get_session():
        with pytest.raises(IntegrityError):
            await run_repo.create(s, trigger="manual", dry_run=False)
        await s.rollback()
