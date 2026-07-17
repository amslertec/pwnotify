"""Reale Writer im tenant-scoped Kontext (Phase 3, Task 1).

Nachfolger der Phase-1-Tests gegen den server_default-Bridge (siehe Review Fix 1/2): der
server_default ist weg (Migration d5e6f7a8b9c0), ORM-Writer stempeln tenant_id jetzt aus dem
aktiven Tenant-Kontext (ContextVar, `default_factory=current_tenant_or_none`) statt aus einem
DB-seitigen Default. Diese Tests treiben echte DB-Writes über die Produktionspfade (run_repo,
exclusion_repo, SettingsService) innerhalb eines aktiven Tenant-Kontexts.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from app.db.tenant_context import tenant_scoped_session, use_tenant
from app.repositories import exclusion_repo, run_repo
from app.services.settings_service import SettingsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def temp_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Wegwerf-Tenant über eine eigene, echt committete Verbindung (wie test_isolation_attack.py):
    `tenant_scoped_session` öffnet eine eigene Verbindung auf der App-Engine und sieht daher
    keine uncommitteten Daten aus der savepoint-isolierten `session`-Fixture."""
    async with migrated_engine.connect() as conn:
        tid = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('Writer-Defaults-Test', 'writer-defaults-test', true, now()) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.commit()
        try:
            yield tid
        finally:
            # ON DELETE CASCADE räumt abhängige run/exclusion/setting-Zeilen mit auf.
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": tid})
            await conn.commit()


async def test_run_repo_create_stamps_active_tenant(temp_tenant: int) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        run = await run_repo.create(s, trigger="manual", dry_run=False)

    assert run.tenant_id == temp_tenant


async def test_exclusion_repo_add_stamps_active_tenant(temp_tenant: int) -> None:
    async with tenant_scoped_session(temp_tenant) as s:
        exclusion = await exclusion_repo.add(
            s, kind="user", value="alice@example.com", label="test"
        )

    assert exclusion.tenant_id == temp_tenant


async def test_settings_upsert_updates_existing_row_on_composite_pk(
    temp_tenant: int, session
) -> None:
    async with use_tenant(temp_tenant):
        service = SettingsService(session)
        await service.set("app.public_url", "https://first.example")
        await service.set("app.public_url", "https://second.example")
        value = await service.get("app.public_url")

    assert value == "https://second.example"

    rows = (
        await session.execute(
            text("SELECT count(*) FROM setting WHERE key = 'app.public_url' AND tenant_id = :t"),
            {"t": temp_tenant},
        )
    ).scalar_one()
    assert rows == 1
