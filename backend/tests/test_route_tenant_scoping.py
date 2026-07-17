"""Tests für die Tenant-Session-Dependency (`get_tenant_session`/`TenantSessionDep`) und die
Core-`pg_insert`-Writer, die `tenant_id` jetzt explizit stempeln (Phase 3, Task 3).

Es gibt in dieser Suite keine HTTP-Route-Tests (kein `TestClient`-Aufbau) -- der Beweis läuft
auf Dependency-Ebene: `get_tenant_session()` wird direkt getrieben (async-Generator), genau
wie FastAPI es beim Request-Teardown tun würde. Seed-Pattern wie in `test_isolation_attack.py`:
echte Superuser-Connection auf `migrated_engine`, echt committet, Cleanup im `finally` (die
savepoint-isolierte `session`-Fixture eignet sich hier nicht, siehe Kommentar dort).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator

import pytest_asyncio
from app.api.deps import default_tenant_id, get_tenant_session
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.repositories import entra_repo, notification_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Unabhängige Kontrollabfrage (nicht über den Modul-Cache in `deps.py`)."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


@pytest_asyncio.fixture
async def foreign_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Ein zweiter, NICHT-Default-Tenant -- muss der Default-Tenant-Session unsichtbar bleiben."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('RtsForeign','rts-foreign',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield tid
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


async def test_get_tenant_session_runs_as_app_role_with_default_tenant_guc(
    migrated_engine: AsyncEngine,
) -> None:
    """Der eigentliche Beweis: die Dependency wechselt in die eingeschränkte App-Rolle und
    setzt das Tenant-GUC auf den echten Default-Tenant -- nicht auf den Owner."""
    dtid = await _real_default_tenant_id(migrated_engine)
    gen = get_tenant_session()
    try:
        session = await anext(gen)
        role, guc = (
            await session.execute(
                text("SELECT current_user, current_setting('app.current_tenant', true)")
            )
        ).one()
        assert role == "pwnotify_app", f"Läuft nicht als App-Rolle: {role}"
        assert guc == str(dtid), f"GUC zeigt nicht auf den Default-Tenant: {guc} != {dtid}"
    finally:
        await gen.aclose()


async def test_default_tenant_id_helper_matches_real_default_tenant(
    migrated_engine: AsyncEngine,
) -> None:
    dtid = await _real_default_tenant_id(migrated_engine)
    async with get_session_factory()() as owner:
        cached = await default_tenant_id(owner)
    assert cached == dtid


async def test_get_tenant_session_sees_only_default_tenant_rows(
    migrated_engine: AsyncEngine, foreign_tenant: int
) -> None:
    """Cross-Tenant-Seed: eine Zeile für den echten Default-Tenant, eine für einen fremden
    Tenant. Über `get_tenant_session()` darf NUR die Default-Tenant-Zeile sichtbar sein."""
    dtid = await _real_default_tenant_id(migrated_engine)
    async with migrated_engine.connect() as conn:
        ids = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO run "
                        "(tenant_id, trigger, dry_run, status, started_at, "
                        "checked_users, sent, failed, skipped, detail_log) VALUES "
                        "(:own,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb), "
                        "(:foreign,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb) "
                        "RETURNING id"
                    ),
                    {"own": dtid, "foreign": foreign_tenant},
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        own_id, foreign_id = ids
        try:
            gen = get_tenant_session()
            try:
                session = await anext(gen)
                rows = (
                    (
                        await session.execute(
                            text("SELECT id FROM run WHERE id IN (:a, :b)"),
                            {"a": own_id, "b": foreign_id},
                        )
                    )
                    .scalars()
                    .all()
                )
            finally:
                await gen.aclose()
            assert set(rows) == {own_id}, f"Erwartet nur die eigene Zeile, sah {rows}"
        finally:
            await conn.execute(
                text("DELETE FROM run WHERE id IN (:a, :b)"), {"a": own_id, "b": foreign_id}
            )
            await conn.commit()


async def test_entra_repo_upsert_stamps_tenant_id_in_tenant_context(
    migrated_engine: AsyncEngine,
) -> None:
    """`entra_repo.upsert` nutzt Core-`pg_insert` und muss `tenant_id` daher explizit
    aus dem aktiven Tenant-Kontext setzen -- der ORM-`default_factory` greift hier nicht."""
    dtid = await _real_default_tenant_id(migrated_engine)
    entra_id = "rts-test-entra-upsert-1"
    async with tenant_scoped_session(dtid) as s:
        await entra_repo.upsert(
            s,
            {
                "entra_id": entra_id,
                "upn": "rts-upsert@example.com",
                "display_name": "RTS Upsert Test",
                "other_mails": [],
                "account_enabled": True,
                "password_never_expires": False,
                "excluded": False,
                "is_shared": False,
                "raw": {},
                "last_synced_at": dt.datetime.now(dt.UTC),
            },
        )
        await s.commit()
    async with migrated_engine.connect() as conn:
        try:
            row = (
                await conn.execute(
                    text("SELECT tenant_id FROM entra_user WHERE entra_id = :eid"),
                    {"eid": entra_id},
                )
            ).one()
            assert row.tenant_id == dtid, f"tenant_id nicht gestempelt: {row.tenant_id}"
        finally:
            await conn.execute(
                text("DELETE FROM entra_user WHERE entra_id = :eid"), {"eid": entra_id}
            )
            await conn.commit()


async def test_notification_repo_record_stamps_tenant_id_in_tenant_context(
    migrated_engine: AsyncEngine,
) -> None:
    """`notification_repo.record` nutzt ebenfalls Core-`pg_insert` und muss `tenant_id`
    explizit setzen -- sonst schlägt die NOT-NULL-Spalte fehl (kein stiller Fallback)."""
    dtid = await _real_default_tenant_id(migrated_engine)
    async with migrated_engine.connect() as conn:
        entra_user_id = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO entra_user "
                        "(tenant_id, entra_id, upn, display_name, other_mails, "
                        "account_enabled, password_never_expires, excluded, is_shared, "
                        "raw, last_synced_at) VALUES "
                        "(:tid, 'rts-test-entra-notif-1', 'rts-notif@example.com', '', "
                        "'[]'::jsonb, true, false, false, false, '{}'::jsonb, now()) "
                        "RETURNING id"
                    ),
                    {"tid": dtid},
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            async with tenant_scoped_session(dtid) as s:
                await notification_repo.record(
                    s,
                    {
                        "entra_user_id": entra_user_id,
                        "run_id": None,
                        "reminder_day": 7,
                        "expiry_cycle": "2026-01-01",
                        "channel": "primary",
                        "backend": "smtp",
                        "recipient": "rts-notif@example.com",
                        "language": "de",
                        "status": "sent",
                        "error": None,
                        "created_at": dt.datetime.now(dt.UTC),
                    },
                )
                await s.commit()

            row = (
                await conn.execute(
                    text("SELECT tenant_id FROM notification_log WHERE entra_user_id = :eid"),
                    {"eid": entra_user_id},
                )
            ).one()
            assert row.tenant_id == dtid, f"tenant_id nicht gestempelt: {row.tenant_id}"
        finally:
            await conn.execute(
                text("DELETE FROM notification_log WHERE entra_user_id = :eid"),
                {"eid": entra_user_id},
            )
            await conn.execute(
                text("DELETE FROM entra_user WHERE id = :eid"), {"eid": entra_user_id}
            )
            await conn.commit()
