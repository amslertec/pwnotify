"""TDD für Task 3 der Access-Modell/Superadmin-Phase: die Access-Seite (`admin_users.py`)
gescopt statt instanzweit.

**THE bug, den dieser Test beweist:** `list_users` rief vormals `user_repo.list_all(session)`
instanzweit auf -- JEDER Tenant sah dieselbe volle Kontoliste (Leselecke). Die Tests hier
seeden zwei Tenants A und B (je ein SSO-Admin + SSO-Auditor) und beweisen NON-VAKUOS, dass
ein an A gebundener lokaler Admin NIE ein Konto von B sieht (B wird tatsächlich befüllt,
nicht nur behauptet).

Treibt die Route-Funktionen direkt an (wie `test_admin_tenants.py`) -- die Routen öffnen
selbst keine zusätzliche Session (kein `tenant_scoped_session`/eigene Verbindung), die
gewöhnliche savepoint-isolierte `session`-Fixture (echtes Postgres, siehe `conftest.py`)
genügt: der äussere Rollback macht die Suite ohne manuelles Aufräumen rückstandsfrei,
zweimal hintereinander ausführbar.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.routes.admin_users import create_local, delete_user, list_users, set_role
from app.core.errors import ForbiddenError
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.auth import AdminUserCreate, RoleUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t3-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, slug: str | None = None) -> Tenant:
    return await tenant_repo.create(session, name=slug or "T3 Tenant", slug=slug or _slug())


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=f"t3-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


class _Seed:
    a_id: int
    b_id: int
    superadmin: AppUser
    local_admin_a: AppUser
    sso_admin_a: AppUser
    sso_auditor_a: AppUser
    sso_admin_b: AppUser
    sso_auditor_b: AppUser


async def _seed(session: AsyncSession) -> _Seed:
    """Zwei Tenants A und B, je mit einem SSO-Admin + SSO-Auditor (B wird also WIRKLICH
    befüllt -- non-vakuöser Beweis, dass B nie an A leckt). Ein lokaler Admin NUR auf A
    zugewiesen (`admin_tenant`), ein Superadmin."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None

    superadmin = await _mk_user(session, role="superadmin")

    local_admin_a = await _mk_user(session, role="admin")
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=a.id))

    sso_admin_a = await _mk_user(session, role="admin", is_sso=True, tenant_id=a.id)
    sso_auditor_a = await _mk_user(session, role="auditor", is_sso=True, tenant_id=a.id)
    sso_admin_b = await _mk_user(session, role="admin", is_sso=True, tenant_id=b.id)
    sso_auditor_b = await _mk_user(session, role="auditor", is_sso=True, tenant_id=b.id)
    await session.flush()

    seed = _Seed()
    seed.a_id, seed.b_id = a.id, b.id
    seed.superadmin = superadmin
    seed.local_admin_a = local_admin_a
    seed.sso_admin_a = sso_admin_a
    seed.sso_auditor_a = sso_auditor_a
    seed.sso_admin_b = sso_admin_b
    seed.sso_auditor_b = sso_auditor_b
    return seed


# ---- list_users: gescopt pro Rolle --------------------------------------------------------- #


async def test_local_admin_a_sees_only_a_accounts_no_superadmins_key(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    out = await list_users(seed.local_admin_a, session)  # type: ignore[arg-type]

    assert "superadmins" not in out
    sso_ids = {u.id for u in out["sso"]}
    local_ids = {u.id for u in out["local"]}

    # A's Konten müssen da sein ...
    assert seed.sso_admin_a.id in sso_ids
    assert seed.sso_auditor_a.id in sso_ids
    assert seed.local_admin_a.id in local_ids

    # ... B's Konten dürfen NIE erscheinen (non-vakuöser Beweis: B ist tatsächlich befüllt).
    assert seed.sso_admin_b.id not in sso_ids
    assert seed.sso_auditor_b.id not in sso_ids

    # Kein Superadmin taucht jemals in der lokalen Liste eines Nicht-Superadmins auf.
    assert seed.superadmin.id not in local_ids


async def test_superadmin_sees_everyone_and_the_superadmins_key(session: AsyncSession) -> None:
    seed = await _seed(session)

    out = await list_users(seed.superadmin, session)  # type: ignore[arg-type]

    assert "superadmins" in out
    superadmin_ids = {u.id for u in out["superadmins"]}
    assert seed.superadmin.id in superadmin_ids
    # Die Superadmin-Liste enthält NIE Nicht-Superadmins.
    assert seed.local_admin_a.id not in superadmin_ids

    local_ids = {u.id for u in out["local"]}
    sso_ids = {u.id for u in out["sso"]}
    assert seed.local_admin_a.id in local_ids
    # Superadmins tauchen nicht nochmal in "local" auf.
    assert seed.superadmin.id not in local_ids
    assert {
        seed.sso_admin_a.id,
        seed.sso_auditor_a.id,
        seed.sso_admin_b.id,
        seed.sso_auditor_b.id,
    } <= sso_ids


async def test_unassigned_local_admin_sees_nothing(session: AsyncSession) -> None:
    """Default-Deny: ein lokaler Admin OHNE JEDE `admin_tenant`-Zuweisung sieht -- anders
    als vor dem Fix -- nicht die volle Liste, sondern gar nichts."""
    unassigned = await _mk_user(session, role="admin")
    out = await list_users(unassigned, session)  # type: ignore[arg-type]
    assert out == {"local": [], "sso": []}


async def test_auditor_caller_gets_empty_scoped_lists(session: AsyncSession) -> None:
    """Default-Deny auch für den Auditor, obwohl die `/access`-Seite im Frontend
    admin-only ist -- dieses Gate gilt am Endpunkt selbst, unabhängig davon."""
    seed = await _seed(session)
    auditor = await _mk_user(session, role="auditor")
    assert auditor.id is not None
    session.add(AuditorTenant(user_id=auditor.id, tenant_id=seed.a_id))
    await session.flush()

    out = await list_users(auditor, session)  # type: ignore[arg-type]
    assert out == {"local": [], "sso": []}


# ---- create_local: Scoping + Auto-Grant ---------------------------------------------------- #


async def test_local_admin_creates_auditor_grants_auditor_tenant_on_active_tenant(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-new-auditor-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="auditor",
    )

    out = await create_local(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        body,
        session,
        seed.a_id,
    )

    assert out.role == "auditor"
    row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == seed.a_id
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "Neuer Auditor hat keine auditor_tenant(A)-Zuweisung erhalten"

    # NIE eine admin_tenant-Zeile (Grant-Typ muss zur Rolle passen) und NIE B.
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None
    b_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == seed.b_id
            )
        )
    ).scalar_one_or_none()
    assert b_row is None

    # Erscheint danach in A's gescopter Liste.
    listed = await list_users(seed.local_admin_a, session)  # type: ignore[arg-type]
    assert out.id in {u.id for u in listed["local"]}


async def test_local_admin_creates_admin_grants_admin_tenant_on_active_tenant(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-new-admin-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="admin",
    )

    out = await create_local(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        body,
        session,
        seed.a_id,
    )

    assert out.role == "admin"
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == seed.a_id
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "Neuer Admin hat keine admin_tenant(A)-Zuweisung erhalten"

    auditor_row = (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert auditor_row is None


async def test_local_admin_without_active_tenant_is_rejected(session: AsyncSession) -> None:
    """Kein `active_tenant`-Claim -> klare Ablehnung statt eines unsichtbaren,
    nicht zugewiesenen Kontos."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-orphan-{uuid.uuid4().hex[:8]}", password="a-strong-password-1", role="admin"
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, seed.local_admin_a, body, session, None)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_required"


async def test_local_admin_cannot_scope_creation_to_unheld_tenant(session: AsyncSession) -> None:
    """Ein gefälschter/veralteter `active_tenant`-Claim auf B (den A's lokaler Admin nicht
    hält) wird -- trotz des rohen Claims -- über `tenant_repo.is_allowed` abgewiesen."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-rogue-{uuid.uuid4().hex[:8]}", password="a-strong-password-1", role="admin"
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, seed.local_admin_a, body, session, seed.b_id)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_required"


async def test_superadmin_creates_user_unrestricted_without_auto_grant(
    session: AsyncSession,
) -> None:
    """Superadmin-Aufrufer: uneingeschränkt, KEINE automatische Zuweisung (Task 4 weist
    Tenants gezielt zu) -- funktioniert sogar ganz ohne `active_tenant`."""
    seed = await _seed(session)
    body = AdminUserCreate(
        username=f"t3-super-created-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="admin",
    )
    out = await create_local(None, seed.superadmin, body, session, None)  # type: ignore[arg-type]

    assert out.role == "admin"
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None
    auditor_row = (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert auditor_row is None


# ---- set_role / delete_user: Cross-Tenant-Fix (Whole-Branch-Review) ------------------------ #
#
# THE bug, den dieser Block beweist: Task 3 hat `list_users`/`create_local` gescopt, aber
# `set_role`/`delete_user` blieben nur über `AdminUser` gegatet (jeder Admin/Superadmin JEDER
# Tenant) und lösten `target` ohne RLS auf `app_user` (instanzweit) auf -- ein lokaler Admin
# von Tenant A konnte so die Rolle eines NUR-zu-B-gehörenden Kontos ändern oder es löschen
# (IDs sind sequentiell enumerierbar). Non-vakuöser Beweis: B (bzw. ein Konto in BEIDEN
# Tenants) wird tatsächlich befüllt und existiert nach dem abgelehnten Versuch unverändert
# weiter -- nicht nur behauptet.


async def test_local_admin_a_cannot_delete_b_only_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, seed.local_admin_a, seed.sso_auditor_b.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "user_not_in_scope"
    assert await session.get(AppUser, seed.sso_auditor_b.id) is not None


async def test_local_admin_a_cannot_set_role_on_b_only_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    with pytest.raises(ForbiddenError) as exc_info:
        await set_role(
            None,  # type: ignore[arg-type]
            seed.local_admin_a,
            seed.sso_auditor_b.id,
            RoleUpdate(role="admin"),
            session,
        )
    assert exc_info.value.code == "user_not_in_scope"
    refreshed = await session.get(AppUser, seed.sso_auditor_b.id)
    assert refreshed is not None
    assert refreshed.role == "auditor"


async def test_local_admin_a_can_still_delete_own_tenant_user(session: AsyncSession) -> None:
    """Regressionsschutz: die neue Scope-Prüfung sperrt nur FREMDE Tenants -- innerhalb des
    eigenen Bereichs bleibt der lokale Admin voll handlungsfähig."""
    seed = await _seed(session)

    out = await delete_user(None, seed.local_admin_a, seed.sso_auditor_a.id, session)  # type: ignore[arg-type]
    assert out.message
    assert await session.get(AppUser, seed.sso_auditor_a.id) is None


async def test_local_admin_a_can_still_set_role_on_own_tenant_user(session: AsyncSession) -> None:
    seed = await _seed(session)

    out = await set_role(
        None,  # type: ignore[arg-type]
        seed.local_admin_a,
        seed.sso_auditor_a.id,
        RoleUpdate(role="admin"),
        session,
    )
    assert out.role == "admin"


async def test_user_in_both_tenants_rejected_for_admin_holding_only_one(
    session: AsyncSession,
) -> None:
    """Teilmengen-Regel (nicht Schnittmenge): ein Konto mit `admin_tenant`-Grants auf A UND B
    darf NICHT von einem Aufrufer angetastet werden, der nur A hält -- eine Löschung/
    Rollenänderung würde sonst auch B ungewollt mittreffen, weil `app_user` instanzweit ist."""
    seed = await _seed(session)
    both = await _mk_user(session, role="admin")
    assert both.id is not None
    session.add(AdminTenant(user_id=both.id, tenant_id=seed.a_id))
    session.add(AdminTenant(user_id=both.id, tenant_id=seed.b_id))
    await session.flush()

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, seed.local_admin_a, both.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "user_not_in_scope"
    assert await session.get(AppUser, both.id) is not None

    with pytest.raises(ForbiddenError) as exc_info2:
        await set_role(
            None,  # type: ignore[arg-type]
            seed.local_admin_a,
            both.id,
            RoleUpdate(role="auditor"),
            session,
        )
    assert exc_info2.value.code == "user_not_in_scope"
    refreshed = await session.get(AppUser, both.id)
    assert refreshed is not None
    assert refreshed.role == "admin"


async def test_superadmin_can_delete_and_set_role_across_tenants(session: AsyncSession) -> None:
    """Superadmin-Aufrufer: uneingeschränkte Reichweite -- die neue Scope-Prüfung gilt
    NICHT für ihn (bestehende Last-Superadmin-/Superadmin-Ziel-Guards bleiben unberührt, sie
    betreffen hier nicht-superadmin Ziele)."""
    seed = await _seed(session)

    out_role = await set_role(
        None,  # type: ignore[arg-type]
        seed.superadmin,
        seed.sso_auditor_b.id,
        RoleUpdate(role="admin"),
        session,
    )
    assert out_role.role == "admin"

    out_delete = await delete_user(None, seed.superadmin, seed.sso_admin_b.id, session)  # type: ignore[arg-type]
    assert out_delete.message
    assert await session.get(AppUser, seed.sso_admin_b.id) is None
