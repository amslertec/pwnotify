"""TDD für den Schutz vor Aussperrung PRO MANDANT (A4).

**THE bug, den dieser Test beweist:** der Letzter-Admin-Guard in `set_role` zählt über
`user_repo.count_admins` INSTANZWEIT -- er verhindert nur, dass die Instanz GAR keinen Admin
mehr hat, nicht dass ein EINZELNER Kunde seinen letzten (Schreib-)Admin verliert. `delete_user`
hatte gar keinen Admin-Zähl-Guard. Angriffskette: Kunde A hat zwei Admins a1/a2, ein Admin b1 in
Kunde B hält die instanzweite Zahl > 1. a1 degradiert/löscht a2, dann sich selbst -> A hat null
Schreib-Admins, nur der Provider-Superadmin kann noch retten.

Der Fix zählt PRO TENANT (`user_repo.count_tenant_admins`) und blockiert die Degradierung/
Löschung des letzten Admins eines Kunden mit `code="last_tenant_admin"` -- auch wenn instanzweit
noch andere Admins existieren.

Treibt die Route-Funktionen direkt an (wie `test_admin_users_scoping.py`); die savepoint-
isolierte `session`-Fixture (echtes Postgres) macht die Suite rückstandsfrei. Der Aufrufer ist
durchweg ein Superadmin -- er überspringt die Scope-Prüfung, sodass der hier zu testende
per-Tenant-Guard sauber erreicht wird (die Cross-Tenant-Scope-Prüfung selbst deckt
`test_admin_users_scoping.py` ab)."""

from __future__ import annotations

import uuid

import pytest
from app.api.routes.admin_users import delete_user, set_role
from app.core.errors import ConflictError
from app.models.tenant import AdminTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo, user_repo
from app.schemas.auth import RoleUpdate
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"a4-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="A4 Tenant", slug=_slug())


async def _mk_admin(
    session: AsyncSession,
    *,
    tenant_id: int,
    role: str = "admin",
    is_sso: bool = False,
    is_active: bool = True,
    grant_tenant_id: int | None = None,
) -> AppUser:
    """Lokaler (oder SSO-)Account, optional mit einer `admin_tenant`-Zuweisung. `grant_tenant_id`
    steuert die Grant-Zeile; ohne sie (SSO-Heim-Admin) trägt der Account nur seine `tenant_id`."""
    u = AppUser(
        username=f"a4-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        is_active=is_active,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    assert u.id is not None
    if grant_tenant_id is not None:
        session.add(AdminTenant(user_id=u.id, tenant_id=grant_tenant_id))
        await session.flush()
    return u


async def _superadmin(session: AsyncSession) -> AppUser:
    u = AppUser(
        username=f"a4-super-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="superadmin",
        is_sso=False,
        tenant_id=None,
    )
    session.add(u)
    await session.flush()
    return u


# ---- count_tenant_admins: die Zähl-Semantik ---------------------------------------------- #


async def test_count_tenant_admins_counts_grants_and_sso_home_not_superadmin_not_inactive(
    session: AsyncSession,
) -> None:
    """`count_tenant_admins(A)` zählt: lokale Admins mit `admin_tenant(A)`-Grant PLUS SSO-Admins
    mit Heim-Tenant A -- aber NIE Superadmins (instanzweit, separat geschützt) und NIE inaktive/
    pending Konten (die können niemanden verwalten). Non-vakuöser Beweis: jede ausgeschlossene
    Kategorie ist tatsächlich befüllt."""
    a = await _mk_tenant(session)
    assert a.id is not None

    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)  # lokaler Admin mit Grant
    await _mk_admin(session, tenant_id=a.id, is_sso=True)  # SSO-Admin heim an A (kein Grant)
    # Ausgeschlossen:
    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id, is_active=False)  # inaktiv
    sa = await _superadmin(session)  # Superadmin -- zählt nie
    session.add(AdminTenant(user_id=sa.id, tenant_id=a.id))  # selbst mit Grant nicht mitzählen
    await session.flush()

    assert await user_repo.count_tenant_admins(session, a.id) == 2


# ---- set_role: per-Tenant-Letzter-Admin-Guard -------------------------------------------- #


async def test_demote_last_tenant_admin_blocked_even_though_instance_count_gt_1(
    session: AsyncSession,
) -> None:
    """A hat NUR a1, B hat b1 (instanzweite Admin-Zahl = 2 > 1, der alte Guard greift nicht).
    Degradierung von a1 muss trotzdem mit `last_tenant_admin` scheitern."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)  # b1 hält instanzweite Zahl > 1
    caller = await _superadmin(session)

    with pytest.raises(ConflictError) as exc_info:
        await set_role(None, caller, a1.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"

    refreshed = await session.get(AppUser, a1.id)
    assert refreshed is not None and refreshed.role == "admin"


async def test_demote_one_of_two_then_last_is_blocked(session: AsyncSession) -> None:
    """Positiv-Kontrolle + volle Angriffskette: solange A ZWEI Admins hat, ist die Degradierung
    des EINEN erlaubt; die des dann verbliebenen letzten wird blockiert."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    a2 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)

    # Positiv: A hat zwei Admins -> a2 darf herabgestuft werden.
    out = await set_role(None, caller, a2.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert out.role == "auditor"

    # Jetzt ist a1 der letzte Admin von A -> Degradierung blockiert.
    with pytest.raises(ConflictError) as exc_info:
        await set_role(None, caller, a1.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"


async def test_demote_auditor_target_never_triggers_tenant_admin_guard(
    session: AsyncSession,
) -> None:
    """Regressionsschutz: ein Auditor-Ziel hat keine `admin_tenant`-Grants -- die Beförderung/
    Änderung eines Auditors darf nie am per-Tenant-Admin-Guard hängen bleiben."""
    a = await _mk_tenant(session)
    assert a.id is not None
    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)  # A behält einen Admin
    auditor = await _mk_admin(session, tenant_id=a.id, role="auditor")
    caller = await _superadmin(session)

    out = await set_role(None, caller, auditor.id, RoleUpdate(role="admin"), session)  # type: ignore[arg-type]
    assert out.role == "admin"


# ---- delete_user: per-Tenant-Letzter-Admin-Guard ----------------------------------------- #


async def test_delete_last_tenant_admin_blocked_even_though_instance_count_gt_1(
    session: AsyncSession,
) -> None:
    """`delete_user` hatte gar keinen Admin-Zähl-Guard: die Löschung des letzten A-Admins muss
    jetzt mit `last_tenant_admin` scheitern, obwohl instanzweit noch b1 existiert."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)

    with pytest.raises(ConflictError) as exc_info:
        await delete_user(None, caller, a1.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"
    assert await session.get(AppUser, a1.id) is not None


async def test_delete_one_of_two_then_last_is_blocked(session: AsyncSession) -> None:
    """Positiv-Kontrolle + Angriffskette für die Löschung: einer von zwei A-Admins darf gelöscht
    werden, der dann verbliebene letzte nicht mehr."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    a2 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)

    out = await delete_user(None, caller, a2.id, session)  # type: ignore[arg-type]
    assert out.message
    assert await session.get(AppUser, a2.id) is None

    with pytest.raises(ConflictError) as exc_info:
        await delete_user(None, caller, a1.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"
    assert await session.get(AppUser, a1.id) is not None
