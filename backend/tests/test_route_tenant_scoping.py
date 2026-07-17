"""Tests für die Tenant-Session-Dependency (`get_tenant_session`/`TenantSessionDep`) und die
Core-`pg_insert`-Writer, die `tenant_id` jetzt explizit stempeln (Phase 3, Task 3).

Phase 4a Task 3: `get_tenant_session` autorisiert jetzt den aktuellen Benutzer (statt immer
blind den Default-Tenant zu liefern) -- ein anonymer Aufruf wie vor diesem Task ist nicht
mehr möglich. Die Tests hier treiben deshalb den vollen, echten Pfad: ein committeter
lokaler Admin (`local_admin`-Fixture) + ein echtes Access-Token dafür + `get_current_user`
davor, exakt wie FastAPI es pro Request tun würde (`_tenant_session_for` unten). Ein lokaler
Admin ohne `active_tenant`-Claim im Token löst über den Fallback (`resolve_initial_tenant`)
auf den Default-Tenant auf -- das ist weiterhin der Beweis, den diese Suite führt, jetzt nur
authentifiziert statt anonym. Die eigentliche Autorisierungs-/Angriffs-Suite (erlaubter vs.
verweigerter Claim) liegt in `test_active_tenant_resolution.py`.

Es gibt in dieser Suite keine HTTP-Route-Tests (kein `TestClient`-Aufbau) -- der Beweis läuft
auf Dependency-Ebene. Seed-Pattern wie in `test_isolation_attack.py`: echte Superuser-
Connection auf `migrated_engine`, echt committet, Cleanup im `finally` (die savepoint-
isolierte `session`-Fixture eignet sich hier nicht, siehe Kommentar dort).
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import AsyncGenerator

import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, default_tenant_id, get_current_user, get_tenant_session
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.repositories import entra_repo, notification_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Unabhängige Kontrollabfrage (nicht über den Modul-Cache in `deps.py`)."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


class _FakeRequest:
    """Duck-typed Request -- `get_current_user`/`get_tenant_session` lesen nur `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


@pytest_asyncio.fixture
async def local_admin(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Ein echter, committeter lokaler Admin -- `get_tenant_session` braucht seit Task 3
    einen authentifizierten, autorisierten Benutzer (kein anonymer Aufruf mehr)."""
    async with migrated_engine.connect() as conn:
        uid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user "
                        "(username, password_hash, role, is_active, is_sso, "
                        "failed_login_count, language, created_at, updated_at) VALUES "
                        "('rts-admin@local', 'x', 'admin', true, false, 0, 'de', now(), now()) "
                        "RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield uid
        finally:
            await conn.execute(text("DELETE FROM app_user WHERE id = :uid"), {"uid": uid})
            await conn.commit()


@contextlib.asynccontextmanager
async def _tenant_session_for(uid: int) -> AsyncGenerator[AsyncSession]:
    """Treibt `get_tenant_session` exakt wie FastAPI es pro Request täte: echtes Access-Token
    für `uid` (kein `active_tenant`-Claim -> Fallback über `resolve_initial_tenant`), eine
    Owner-Session für `get_current_user`+Autorisierung, dann die tenant-gescopte Session."""
    pair = issue_token_pair(str(uid))
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session(request, user, owner)
        try:
            yield await anext(gen)
        finally:
            await gen.aclose()


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
    migrated_engine: AsyncEngine, local_admin: int
) -> None:
    """Der eigentliche Beweis: die Dependency wechselt in die eingeschränkte App-Rolle und
    setzt das Tenant-GUC auf den echten Default-Tenant -- nicht auf den Owner. Der lokale
    Admin trägt keinen `active_tenant`-Claim, löst also über den Fallback auf."""
    dtid = await _real_default_tenant_id(migrated_engine)
    async with _tenant_session_for(local_admin) as session:
        role, guc = (
            await session.execute(
                text("SELECT current_user, current_setting('app.current_tenant', true)")
            )
        ).one()
        assert role == "pwnotify_app", f"Läuft nicht als App-Rolle: {role}"
        assert guc == str(dtid), f"GUC zeigt nicht auf den Default-Tenant: {guc} != {dtid}"


async def test_default_tenant_id_helper_matches_real_default_tenant(
    migrated_engine: AsyncEngine,
) -> None:
    dtid = await _real_default_tenant_id(migrated_engine)
    async with get_session_factory()() as owner:
        cached = await default_tenant_id(owner)
    assert cached == dtid


async def test_get_tenant_session_sees_only_default_tenant_rows(
    migrated_engine: AsyncEngine, foreign_tenant: int, local_admin: int
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
            async with _tenant_session_for(local_admin) as session:
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
