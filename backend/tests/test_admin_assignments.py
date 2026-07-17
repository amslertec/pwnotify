"""TDD für Task 4 der Access-Modell/Superadmin-Phase: Zuweisungs-API
(`admin_assignments.py`) + Superadmin-Verwaltung (`admin_users.py`).

Treibt die Route-Funktionen direkt an (wie `test_admin_tenants.py`/
`test_admin_users_scoping.py`) -- die gewöhnliche savepoint-isolierte `session`-Fixture
genügt, kein manuelles Aufräumen nötig (äusserer Rollback macht die Suite rückstandsfrei,
zweimal hintereinander ausführbar).

Kernbeweis (REFINEMENT ggü. dem Task-4-Brief): der Grant-Typ folgt strukturell der ROLLE
des Zielkontos, nicht einer vom Aufrufer frei wählbaren Dual-Liste -- ein `role=='admin'`
Ziel bekommt NIE eine `auditor_tenant`-Zeile und umgekehrt, unabhängig davon, was der
Aufrufer "meinte".
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import require_superadmin
from app.api.routes.admin_assignments import get_assignments, set_assignments
from app.api.routes.admin_users import create_superadmin, delete_user, set_role, set_superadmin
from app.core.errors import ConflictError, ForbiddenError
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.assignment import AssignmentUpdate
from app.schemas.auth import RoleUpdate, SuperadminCreate, SuperadminToggle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t4-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="T4 Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


async def _mk_user(
    session: AsyncSession, *, role: str, is_sso: bool = False, tenant_id: int | None = None
) -> AppUser:
    u = AppUser(
        username=f"t4-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def _admin_row(session: AsyncSession, user_id: int, tenant_id: int) -> AdminTenant | None:
    return (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == user_id, AdminTenant.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


async def _auditor_row(session: AsyncSession, user_id: int) -> AuditorTenant | None:
    return (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == user_id))
    ).scalar_one_or_none()


# ---- Reconcile: admin-role target --------------------------------------------------------- #


async def test_put_assigns_admin_tenants_and_grants_write_access(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    local_admin = await _mk_user(session, role="admin")
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert local_admin.id is not None and tenant_a.id is not None and tenant_b.id is not None

    out = await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        local_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id, tenant_b.id]),
        session,
    )
    assert set(out.tenant_ids) == {tenant_a.id, tenant_b.id}
    assert await _admin_row(session, local_admin.id, tenant_a.id) is not None
    assert await _admin_row(session, local_admin.id, tenant_b.id) is not None

    # Task 2's admin_tenants() proves real write capacity, not just a raw row.
    caps = await tenant_repo.admin_tenants(session, local_admin)
    assert caps == {tenant_a.id, tenant_b.id}

    # Removing B revokes exactly that grant, keeps A.
    out2 = await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        local_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )
    assert out2.tenant_ids == [tenant_a.id]
    assert await _admin_row(session, local_admin.id, tenant_a.id) is not None
    assert await _admin_row(session, local_admin.id, tenant_b.id) is None

    caps2 = await tenant_repo.admin_tenants(session, local_admin)
    assert caps2 == {tenant_a.id}


async def test_get_assignments_reflects_current_grants(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    local_admin = await _mk_user(session, role="admin")
    tenant_a = await _mk_tenant(session)
    assert local_admin.id is not None and tenant_a.id is not None

    await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        local_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )
    out = await get_assignments(superadmin, local_admin.id, session)  # type: ignore[arg-type]
    assert out.role == "admin"
    assert out.tenant_ids == [tenant_a.id]


# ---- Grant type follows target role, NOT caller choice ------------------------------------ #


async def test_grant_type_follows_target_role_auditor_writes_auditor_tenant(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    local_auditor = await _mk_user(session, role="auditor")
    tenant_a = await _mk_tenant(session)
    assert local_auditor.id is not None and tenant_a.id is not None

    await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        local_auditor.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )

    auditor_row = await _auditor_row(session, local_auditor.id)
    assert auditor_row is not None
    admin_row = await _admin_row(session, local_auditor.id, tenant_a.id)
    assert admin_row is None, "Auditor-Ziel erhielt fälschlich eine admin_tenant-Zeile"


async def test_admin_role_target_never_gets_auditor_tenant_row(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    local_admin = await _mk_user(session, role="admin")
    tenant_a = await _mk_tenant(session)
    assert local_admin.id is not None and tenant_a.id is not None

    await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        local_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )

    assert await _admin_row(session, local_admin.id, tenant_a.id) is not None
    assert await _auditor_row(session, local_admin.id) is None


async def test_sso_admin_of_main_tenant_can_be_granted_another_tenant(
    session: AsyncSession,
) -> None:
    """Design §2/§11.2: ein SSO-Konto des Haupttenants kann zusätzlich auf einen weiteren
    Kunden berechtigt werden -- die Kapazität folgt weiterhin der (Ziel-)Rolle."""
    superadmin = await _mk_user(session, role="superadmin")
    home = await _mk_tenant(session)
    other = await _mk_tenant(session)
    assert home.id is not None and other.id is not None
    sso_admin = await _mk_user(session, role="admin", is_sso=True, tenant_id=home.id)
    assert sso_admin.id is not None

    await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        sso_admin.id,
        AssignmentUpdate(tenant_ids=[other.id]),
        session,
    )
    caps = await tenant_repo.admin_tenants(session, sso_admin)
    assert caps == {home.id, other.id}


# ---- Guard rails --------------------------------------------------------------------------- #


async def test_put_targeting_superadmin_is_rejected(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    other_superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    assert other_superadmin.id is not None and tenant_a.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            other_superadmin.id,
            AssignmentUpdate(tenant_ids=[tenant_a.id]),
            session,
        )
    assert exc_info.value.code == "cannot_assign_superadmin"


async def test_put_assigning_inactive_tenant_is_rejected(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    local_admin = await _mk_user(session, role="admin")
    inactive = await _mk_tenant(session, active=False)
    assert local_admin.id is not None and inactive.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            local_admin.id,
            AssignmentUpdate(tenant_ids=[inactive.id]),
            session,
        )
    assert exc_info.value.code == "tenant_not_active"
    assert await _admin_row(session, local_admin.id, inactive.id) is None


async def test_non_superadmin_cannot_call_any_assignment_route(session: AsyncSession) -> None:
    local_admin = await _mk_user(session, role="admin")
    target = await _mk_user(session, role="auditor")
    tenant_a = await _mk_tenant(session)
    assert target.id is not None and tenant_a.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        gate = await require_superadmin(local_admin)
        await get_assignments(gate, target.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_required"

    with pytest.raises(ForbiddenError) as exc_info:
        gate = await require_superadmin(local_admin)
        await set_assignments(
            None,  # type: ignore[arg-type]
            gate,
            target.id,
            AssignmentUpdate(tenant_ids=[tenant_a.id]),
            session,
        )
    assert exc_info.value.code == "superadmin_required"
    assert await _admin_row(session, target.id, tenant_a.id) is None


# ---- Superadmin creation -------------------------------------------------------------------- #


async def test_create_superadmin_makes_local_superadmin_account(session: AsyncSession) -> None:
    caller = await _mk_user(session, role="superadmin")
    body = SuperadminCreate(
        username=f"t4-new-super-{uuid.uuid4().hex[:8]}", password="a-strong-password-1"
    )
    out = await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert out.role == "superadmin"
    assert out.is_sso is False


async def test_create_superadmin_rejects_sso_flag(session: AsyncSession) -> None:
    caller = await _mk_user(session, role="superadmin")
    body = SuperadminCreate(
        username=f"t4-sso-super-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        is_sso=True,
    )
    with pytest.raises(ConflictError) as exc_info:
        await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_must_be_local"


async def test_non_superadmin_cannot_create_superadmin(session: AsyncSession) -> None:
    local_admin = await _mk_user(session, role="admin")
    body = SuperadminCreate(
        username=f"t4-rogue-super-{uuid.uuid4().hex[:8]}", password="a-strong-password-1"
    )
    with pytest.raises(ForbiddenError) as exc_info:
        gate = await require_superadmin(local_admin)
        await create_superadmin(None, gate, body, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_required"


# ---- Promote / demote / delete guard rails -------------------------------------------------- #


async def test_demote_last_active_superadmin_is_rejected(session: AsyncSession) -> None:
    only_superadmin = await _mk_user(session, role="superadmin")
    assert only_superadmin.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_superadmin(
            None,  # type: ignore[arg-type]
            only_superadmin,
            only_superadmin.id,
            SuperadminToggle(promote=False),
            session,
        )
    assert exc_info.value.code == "cannot_demote_last_superadmin"


async def test_delete_last_active_superadmin_is_rejected(session: AsyncSession) -> None:
    """Der einzige AKTIVE Superadmin ist der Aufrufer selbst -- ein zweites Superadmin-
    Konto existiert, ist aber deaktiviert (`is_active=False`) und zählt daher nicht in
    `count_superadmins`. Der Aufrufer (aktiver Superadmin, also nicht durch den neuen
    `superadmin_required`-Schutz blockiert, s.u.) darf dieses letzte-aktive-Superadmin-
    Gleichgewicht trotzdem nicht antasten -- `cannot_delete_last_superadmin` greift weiter."""
    caller = await _mk_user(session, role="superadmin")
    inactive_superadmin = await _mk_user(session, role="superadmin")
    assert inactive_superadmin.id is not None
    inactive_superadmin.is_active = False
    await session.flush()

    with pytest.raises(ConflictError) as exc_info:
        await delete_user(None, caller, inactive_superadmin.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "cannot_delete_last_superadmin"
    assert await session.get(AppUser, inactive_superadmin.id) is not None


async def test_plain_admin_cannot_delete_superadmin_target(session: AsyncSession) -> None:
    """Sicherheitsreview-Fix (Task 4): vormals war `delete_user` NUR über den
    Last-Superadmin-Zähler geschützt -- bei 2+ Superadmins konnte ein PLAIN Admin
    (dieses Gate ist `AdminUser`, nicht `SuperadminUser`) einen NICHT-letzten Superadmin
    löschen, wiederholt bis zum letzten. Jetzt: jeder Löschversuch eines Superadmin-Ziels
    durch einen Nicht-Superadmin scheitert hart, unabhängig von der Anzahl."""
    plain_admin = await _mk_user(session, role="admin")
    superadmin_1 = await _mk_user(session, role="superadmin")
    superadmin_2 = await _mk_user(session, role="superadmin")
    assert superadmin_1.id is not None and superadmin_2.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, plain_admin, superadmin_2.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_required"
    assert await session.get(AppUser, superadmin_2.id) is not None
    # 2 Superadmins vorhanden -- keine Last-Superadmin-Ausrede möglich, es ist echt der
    # neue superadmin_required-Schutz, der hier greift.
    assert superadmin_1.role == "superadmin"


async def test_sso_admin_cannot_delete_superadmin_target(session: AsyncSession) -> None:
    sso_admin = await _mk_user(session, role="admin", is_sso=True)
    await _mk_user(session, role="superadmin")
    superadmin_target = await _mk_user(session, role="superadmin")
    assert superadmin_target.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await delete_user(None, sso_admin, superadmin_target.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_required"
    assert await session.get(AppUser, superadmin_target.id) is not None


async def test_superadmin_can_delete_non_last_superadmin(session: AsyncSession) -> None:
    caller = await _mk_user(session, role="superadmin")
    other_superadmin = await _mk_user(session, role="superadmin")
    assert other_superadmin.id is not None

    out = await delete_user(None, caller, other_superadmin.id, session)  # type: ignore[arg-type]
    assert out.message
    assert await session.get(AppUser, other_superadmin.id) is None


async def test_local_admin_can_still_delete_plain_admin_and_auditor(session: AsyncSession) -> None:
    """Regressionsschutz: der neue Superadmin-Schutz betrifft NUR Superadmin-Ziele -- ein
    lokaler Admin darf weiterhin gewöhnliche Admin-/Auditor-Konten löschen, SOFERN sie
    innerhalb seines eigenen Tenant-Bereichs liegen (Cross-Tenant-Fix, s. `test_admin_users_
    scoping.py`) -- alle drei Konten hier sind bewusst auf denselben Tenant A gescopt, damit
    diese Regression von der neuen Scope-Prüfung unberührt bleibt."""
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    caller = await _mk_user(session, role="admin")
    other_admin = await _mk_user(session, role="admin")
    auditor = await _mk_user(session, role="auditor")
    assert caller.id is not None and other_admin.id is not None and auditor.id is not None
    session.add(AdminTenant(user_id=caller.id, tenant_id=tenant_a.id))
    session.add(AdminTenant(user_id=other_admin.id, tenant_id=tenant_a.id))
    session.add(AuditorTenant(user_id=auditor.id, tenant_id=tenant_a.id))
    await session.flush()

    out1 = await delete_user(None, caller, other_admin.id, session)  # type: ignore[arg-type]
    assert out1.message
    assert await session.get(AppUser, other_admin.id) is None

    out2 = await delete_user(None, caller, auditor.id, session)  # type: ignore[arg-type]
    assert out2.message
    assert await session.get(AppUser, auditor.id) is None


async def test_demote_one_of_two_superadmins_succeeds(session: AsyncSession) -> None:
    superadmin_1 = await _mk_user(session, role="superadmin")
    superadmin_2 = await _mk_user(session, role="superadmin")
    assert superadmin_2.id is not None

    out = await set_superadmin(
        None,  # type: ignore[arg-type]
        superadmin_1,
        superadmin_2.id,
        SuperadminToggle(promote=False),
        session,
    )
    assert out.role == "admin"


async def test_plain_admin_calling_set_role_on_superadmin_target_is_forbidden(
    session: AsyncSession,
) -> None:
    plain_admin = await _mk_user(session, role="admin")
    superadmin_target = await _mk_user(session, role="superadmin")
    assert superadmin_target.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await set_role(
            None,  # type: ignore[arg-type]
            plain_admin,
            superadmin_target.id,
            RoleUpdate(role="admin"),
            session,
        )
    assert exc_info.value.code == "superadmin_required"
    assert (await session.get(AppUser, superadmin_target.id)).role == "superadmin"  # type: ignore[union-attr]


async def test_promoting_sso_account_to_superadmin_is_rejected(session: AsyncSession) -> None:
    caller = await _mk_user(session, role="superadmin")
    tenant = await _mk_tenant(session)
    assert tenant.id is not None
    sso_admin = await _mk_user(session, role="admin", is_sso=True, tenant_id=tenant.id)
    assert sso_admin.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_superadmin(
            None,  # type: ignore[arg-type]
            caller,
            sso_admin.id,
            SuperadminToggle(promote=True),
            session,
        )
    assert exc_info.value.code == "superadmin_must_be_local"


async def test_promoting_local_admin_clears_its_tenant_grants(session: AsyncSession) -> None:
    """Dokumentierte Design-Entscheidung: bei der Beförderung werden vorhandene
    `admin_tenant`-Zuweisungen geräumt (Superadmin sieht ohnehin alles, verwaiste
    Zeilen wären Datenmüll)."""
    caller = await _mk_user(session, role="superadmin")
    local_admin = await _mk_user(session, role="admin")
    tenant_a = await _mk_tenant(session)
    assert local_admin.id is not None and tenant_a.id is not None
    session.add(AdminTenant(user_id=local_admin.id, tenant_id=tenant_a.id))
    await session.flush()

    out = await set_superadmin(
        None,  # type: ignore[arg-type]
        caller,
        local_admin.id,
        SuperadminToggle(promote=True),
        session,
    )
    assert out.role == "superadmin"
    assert await _admin_row(session, local_admin.id, tenant_a.id) is None
