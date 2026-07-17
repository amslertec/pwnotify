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
from app.api.deps import require_local_admin
from app.api.routes.admin_tenants import create_tenant, delete_tenant, list_tenants, update_tenant
from app.core.errors import ConflictError, ForbiddenError
from app.models.run import Run
from app.models.tenant import AuditorTenant, Tenant
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


async def _mk_sso_admin(session: AsyncSession, *, tenant_id: int) -> AppUser:
    """SSO-Konto, gebunden an einen Kunden -- `role='admin'`, aber KEIN lokaler Admin (siehe
    `require_local_admin`): genau das Konto, das die Schwachstelle ausnutzen konnte."""
    user = AppUser(
        username=f"t2-sso-admin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=True,
        tenant_id=tenant_id,
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_local_auditor(session: AsyncSession, *, tenant_ids: list[int]) -> AppUser:
    auditor = AppUser(
        username=f"t2-auditor-{uuid.uuid4().hex[:8]}", password_hash="x", role="auditor"
    )
    session.add(auditor)
    await session.flush()
    assert auditor.id is not None
    for tid in tenant_ids:
        session.add(AuditorTenant(user_id=auditor.id, tenant_id=tid))
    await session.flush()
    return auditor


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


# ---- Cross-Tenant-Autorisierung (Whole-Branch-Review Fix, CRITICAL) ------------------------- #
#
# Vorher hingen `create_tenant`/`update_tenant`/`delete_tenant` an `AdminUser` (nur
# `role == "admin"`) und `list_tenants` an `CurrentUser` (jedes eingeloggte Konto). Ein
# SSO-Admin, gebunden an Kunde B, konnte damit ALLE Kunden auflisten (Enumeration) UND
# Kunde A per DELETE hart löschen (Kaskade über Nutzer/Sessions/Daten). Die Tests unten
# müssen gegen die alte Verdrahtung fehlschlagen -- sie beweisen, dass jetzt dieselbe
# Grenze wie in `get_audit_session` gilt: nur der LOKALE Admin ist instanzweit.
#
# Die Routen werden hier -- wie überall in dieser Datei -- direkt als Coroutinen
# aufgerufen, NICHT über einen echten HTTP-Request. Ein direkter Aufruf löst FastAPIs
# `Depends`-Auflösung NICHT aus (das ist reine Request-Routing-Maschinerie) -- die
# `admin: LocalAdminUser`-Annotation an den Routen wäre also wirkungslos, würde man ihr
# einfach ein rohes `AppUser`-Objekt übergeben. Um die Guard-Dependency trotzdem ECHT zu
# prüfen (nicht nur die Routen-Körper), wird `require_local_admin` hier explizit VOR der
# Route aufgerufen -- exakt das, was FastAPI pro Request täte (gleiches Muster wie
# `test_audit_tenant_scope.py`, das `get_audit_session` direkt treibt).


async def test_sso_admin_bound_to_tenant_b_cannot_delete_tenant_a(session: AsyncSession) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    sso_admin_b = await _mk_sso_admin(session, tenant_id=tenant_b.id)

    with pytest.raises(ForbiddenError) as exc_info:
        admin = await require_local_admin(sso_admin_b)
        await delete_tenant(None, admin, tenant_a.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "local_admin_required"

    assert await tenant_repo.get(session, tenant_a.id) is not None


async def test_sso_admin_bound_to_tenant_b_cannot_create_tenant(session: AsyncSession) -> None:
    tenant_b = await _mk_tenant(session)
    assert tenant_b.id is not None
    sso_admin_b = await _mk_sso_admin(session, tenant_id=tenant_b.id)

    with pytest.raises(ForbiddenError) as exc_info:
        admin = await require_local_admin(sso_admin_b)
        await create_tenant(
            None,  # type: ignore[arg-type]
            admin,
            TenantCreate(name="Rogue", slug=_slug()),
            session,
        )
    assert exc_info.value.code == "local_admin_required"


async def test_sso_admin_bound_to_tenant_b_cannot_update_tenant_a(session: AsyncSession) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    sso_admin_b = await _mk_sso_admin(session, tenant_id=tenant_b.id)

    with pytest.raises(ForbiddenError) as exc_info:
        admin = await require_local_admin(sso_admin_b)
        await update_tenant(
            None,  # type: ignore[arg-type]
            admin,
            tenant_a.id,
            TenantUpdate(name="Pwned"),
            session,
        )
    assert exc_info.value.code == "local_admin_required"

    still_there = await tenant_repo.get(session, tenant_a.id)
    assert still_there is not None
    assert still_there.name != "Pwned"


async def test_sso_admin_lists_only_own_tenant_not_full_roster(session: AsyncSession) -> None:
    """Der Kernbeweis für die Leselecke: ein an Kunde B gebundenes SSO-Konto darf NUR Kunde
    B sehen -- nicht Kunde A, nicht den Default-Kunden, nicht die volle Liste."""
    tenant_a = await _mk_tenant(session, slug=_slug())
    tenant_b = await _mk_tenant(session, slug=_slug())
    assert tenant_a.id is not None and tenant_b.id is not None
    sso_admin_b = await _mk_sso_admin(session, tenant_id=tenant_b.id)

    listed = await list_tenants(sso_admin_b, session)  # type: ignore[arg-type]
    slugs = {t.slug for t in listed}

    assert slugs == {tenant_b.slug}, f"SSO-Admin B sah fremde Mandanten: {slugs}"
    assert tenant_a.slug not in slugs, "Cross-Tenant-Leck: SSO-Admin B sah Kunde A"


async def test_local_auditor_lists_only_assigned_tenants(session: AsyncSession) -> None:
    tenant_a = await _mk_tenant(session, slug=_slug())
    tenant_b = await _mk_tenant(session, slug=_slug())
    tenant_c = await _mk_tenant(session, slug=_slug())
    assert tenant_a.id is not None and tenant_b.id is not None and tenant_c.id is not None
    auditor = await _mk_local_auditor(session, tenant_ids=[tenant_b.id])

    listed = await list_tenants(auditor, session)  # type: ignore[arg-type]
    slugs = {t.slug for t in listed}

    assert slugs == {tenant_b.slug}
    assert tenant_a.slug not in slugs
    assert tenant_c.slug not in slugs


async def test_local_auditor_cannot_perform_tenant_writes(session: AsyncSession) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    auditor = await _mk_local_auditor(session, tenant_ids=[tenant_a.id])

    with pytest.raises(ForbiddenError) as exc_info:
        admin = await require_local_admin(auditor)
        await delete_tenant(None, admin, tenant_a.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "local_admin_required"
    assert await tenant_repo.get(session, tenant_a.id) is not None


async def test_local_admin_unaffected_sees_full_roster_and_retains_write_access(
    session: AsyncSession,
) -> None:
    """Regressionsschutz: der lokale Admin (nicht SSO, `role=='admin'`) bleibt instanzweit --
    volle Liste, weiterhin create/update/delete möglich."""
    admin = await _mk_admin(session)
    tenant_a = await _mk_tenant(session, slug=_slug())
    tenant_b = await _mk_tenant(session, slug=_slug())
    assert tenant_a.id is not None and tenant_b.id is not None

    listed = await list_tenants(admin, session)  # type: ignore[arg-type]
    slugs = {t.slug for t in listed}
    assert tenant_a.slug in slugs
    assert tenant_b.slug in slugs

    guarded_admin = await require_local_admin(admin)

    out = await create_tenant(
        None,  # type: ignore[arg-type]
        guarded_admin,
        TenantCreate(name="Local Admin Create", slug=_slug()),
        session,
    )
    assert out.id is not None

    updated = await update_tenant(
        None,  # type: ignore[arg-type]
        guarded_admin,
        tenant_a.id,
        TenantUpdate(name="Renamed"),
        session,
    )
    assert updated.name == "Renamed"

    msg = await delete_tenant(None, guarded_admin, tenant_b.id, session)  # type: ignore[arg-type]
    assert "gelöscht" in msg.message
    assert await tenant_repo.get(session, tenant_b.id) is None
