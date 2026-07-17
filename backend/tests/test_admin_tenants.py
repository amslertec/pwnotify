"""TDD für die Tenant-CRUD-Routen + Schutzregeln (Phase 4c Task 2).

Treibt die Route-Funktionen aus `app.api.routes.admin_tenants` direkt an -- wie
`test_sync_sso_tenant_scope.py` es für `admin_users.sync_sso` tut, nur ohne Notwendigkeit
für eine echt committete Verbindung: diese Routen öffnen selbst KEINE zusätzliche Session
(kein `tenant_scoped_session`/`get_session_factory()` innerhalb der Route) -- alles läuft
über die eine übergebene `session`. Deshalb genügt hier wie in `test_tenant_repo_crud.py`
die gewöhnliche, savepoint-isolierte `session`-Fixture: volle Rückstandsfreiheit über den
Rollback der äusseren Transaktion, kein eigenes Aufräumen nötig.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from app.api.routes.admin_tenants import create_tenant, delete_tenant, list_tenants, update_tenant
from app.core.errors import ConflictError
from app.models.run import Run
from app.models.tenant import Tenant
from app.models.user import AppUser, UserSession
from app.repositories import tenant_repo
from app.schemas.tenant import TenantCreate, TenantUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t2-{uuid.uuid4().hex[:10]}"


async def _mk_admin(session: AsyncSession) -> AppUser:
    admin = AppUser(username=f"t2-admin-{uuid.uuid4().hex[:8]}", password_hash="x", role="admin")
    session.add(admin)
    await session.flush()
    return admin


async def _mk_tenant(session: AsyncSession, *, slug: str | None = None) -> Tenant:
    return await tenant_repo.create(session, name=slug or "T2 Tenant", slug=slug or _slug())


# ---- create ------------------------------------------------------------------------------- #


async def test_create_tenant_appears_in_list_with_sso_user_count(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    slug = _slug()
    out = await create_tenant(
        None,  # type: ignore[arg-type]
        admin,
        TenantCreate(name="Contoso", slug=slug, entra_tenant_id=f"tid-{slug}"),
        session,
    )
    assert out.slug == slug
    assert out.sso_user_count == 0

    listed = await list_tenants(admin, session)  # type: ignore[arg-type]
    assert any(t.slug == slug and t.id == out.id for t in listed)


async def test_create_duplicate_slug_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    slug = _slug()
    await create_tenant(None, admin, TenantCreate(name="A", slug=slug), session)  # type: ignore
    with pytest.raises(ConflictError) as exc_info:
        await create_tenant(None, admin, TenantCreate(name="B", slug=slug), session)  # type: ignore
    assert exc_info.value.code == "tenant_slug_taken"


async def test_create_duplicate_entra_tid_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    tid = f"tid-{uuid.uuid4().hex[:10]}"
    await create_tenant(
        None,
        admin,
        TenantCreate(name="A", slug=_slug(), entra_tenant_id=tid),
        session,  # type: ignore
    )
    with pytest.raises(ConflictError) as exc_info:
        await create_tenant(
            None,
            admin,
            TenantCreate(name="B", slug=_slug(), entra_tenant_id=tid),
            session,  # type: ignore
        )
    assert exc_info.value.code == "tenant_entra_tid_taken"


# ---- update / guard rails ------------------------------------------------------------------ #


async def test_update_default_tenant_deactivate_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    default = await tenant_repo.default_tenant(session)
    with pytest.raises(ConflictError) as exc_info:
        await update_tenant(
            None,
            admin,
            default.id,
            TenantUpdate(is_active=False),
            session,  # type: ignore
        )
    assert exc_info.value.code == "cannot_deactivate_default_tenant"


async def test_update_default_tenant_name_is_allowed(session: AsyncSession) -> None:
    """Nur die Deaktivierung ist gesperrt -- der Name des Default-Kunden darf sich ändern."""
    admin = await _mk_admin(session)
    default = await tenant_repo.default_tenant(session)
    out = await update_tenant(
        None,
        admin,
        default.id,
        TenantUpdate(name="Meine Firma AG"),
        session,  # type: ignore
    )
    assert out.name == "Meine Firma AG"
    assert out.is_active is True


async def test_update_duplicate_entra_tid_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    tid = f"tid-{uuid.uuid4().hex[:10]}"
    await _mk_tenant(session)
    taken_by = await tenant_repo.create(session, name="Taken", slug=_slug(), entra_tenant_id=tid)
    other = await _mk_tenant(session)
    assert taken_by.id is not None and other.id is not None
    with pytest.raises(ConflictError) as exc_info:
        await update_tenant(
            None,
            admin,
            other.id,
            TenantUpdate(entra_tenant_id=tid),
            session,  # type: ignore
        )
    assert exc_info.value.code == "tenant_entra_tid_taken"


# ---- delete / guard rails ------------------------------------------------------------------ #


async def test_delete_default_tenant_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    default = await tenant_repo.default_tenant(session)
    with pytest.raises(ConflictError) as exc_info:
        await delete_tenant(None, admin, default.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "cannot_delete_default_tenant"


async def test_delete_last_active_tenant_raises_conflict(session: AsyncSession) -> None:
    admin = await _mk_admin(session)
    only_active = await _mk_tenant(session)
    assert only_active.id is not None

    # Default-Kunde ausser Betrieb setzen (direkte Mutation, nicht über die Route -- die
    # Route selbst verbietet das ja gerade). `only_active` ist danach der einzige aktive
    # Tenant, unabhängig vom Default-Sonderschutz.
    default = await tenant_repo.default_tenant(session)
    default.is_active = False
    await session.flush()

    with pytest.raises(ConflictError) as exc_info:
        await delete_tenant(None, admin, only_active.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "cannot_delete_last_tenant"


async def test_delete_inactive_tenant_is_not_blocked_by_last_active_guard(
    session: AsyncSession,
) -> None:
    """Ein bereits deaktivierter Kunde zählt nicht als der 'letzte aktive' -- er darf weg,
    auch wenn aktuell nur ein einziger anderer Tenant aktiv ist."""
    admin = await _mk_admin(session)
    victim = await _mk_tenant(session)
    assert victim.id is not None
    updated = await tenant_repo.update(session, victim.id, is_active=False)
    assert updated.is_active is False

    await delete_tenant(None, admin, victim.id, session)  # type: ignore[arg-type]
    assert await tenant_repo.get(session, victim.id) is None


async def test_delete_tenant_cascades_sso_user_sessions_and_data_rows(
    session: AsyncSession,
) -> None:
    """Der Kernbeweis: Löschen eines (nicht-default) Kunden mit gebundenem SSO-Konto räumt
    das Konto samt seiner Sitzung mit auf (sonst Waisenkonto über den SET-NULL-FK) UND
    kaskadiert automatisch in eine der sechs Datentabellen (`run`, ondelete=CASCADE)."""
    admin = await _mk_admin(session)
    victim = await _mk_tenant(session)
    assert victim.id is not None
    vid = victim.id

    sso_user = AppUser(
        username=f"t2-sso-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=True,
        tenant_id=vid,
    )
    session.add(sso_user)
    await session.flush()
    assert sso_user.id is not None
    uid = sso_user.id

    us = UserSession(
        user_id=uid,
        refresh_jti=f"t2-jti-{uuid.uuid4().hex}",
        token_hash="x",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
    )
    session.add(us)

    run = Run(tenant_id=vid, trigger="manual", dry_run=False, status="ok")
    session.add(run)
    await session.flush()
    run_id = run.id

    msg = await delete_tenant(None, admin, vid, session)  # type: ignore[arg-type]
    assert "gelöscht" in msg.message

    assert await tenant_repo.get(session, vid) is None
    assert (
        await session.execute(select(AppUser).where(AppUser.id == uid))
    ).scalar_one_or_none() is None
    assert (
        await session.execute(select(UserSession).where(UserSession.refresh_jti == us.refresh_jti))
    ).scalar_one_or_none() is None
    assert (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none() is None
