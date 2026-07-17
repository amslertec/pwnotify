"""TDD für den VIER-WEGE-Zugriffsmodell-Rewrite (Access-Modell/Superadmin-Phase, Task 2):
`tenant_repo.admin_tenants`/`auditor_tenants`/`allowed_tenant_ids`/`is_allowed` (inkl. des
neuen `write`-Kwargs) und `resolve_initial_tenant` für ALLE VIER Kontoarten (Design §2):

- **Superadmin** (`not is_sso and role=="superadmin"`): instanzweit, alle aktiven Tenants,
  lesend wie schreibend.
- **Lokaler Admin** (`not is_sso and role=="admin"`): NUR seine `admin_tenant`-Grants
  (Kernänderung ggü. dem alten Drei-Wege-Modell -- vorher instanzweit/`None`).
- **Lokaler Auditor** (`not is_sso and role=="auditor"`): NUR seine `auditor_tenant`-Grants,
  nie Schreibzugriff.
- **SSO-Konto**: Heim-`tenant_id` (Kapazität aus der Heim-Rolle) UNION alle
  `admin_tenant`/`auditor_tenant`-Grants -- ein SSO-Konto des Haupttenants kann vom
  Superadmin zusätzlich auf weitere Kunden berechtigt werden.

Läuft auf der gewöhnlichen, savepoint-isolierten `session`-Fixture (Owner-Session, kein
Rollenwechsel, siehe `test_tenant_authorization.py` für die Begründung) -- `tenant_repo`
öffnet keine eigene Verbindung.
"""

from __future__ import annotations

import pytest
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _mk_tenant(session: AsyncSession, *, slug: str, is_active: bool = True) -> Tenant:
    t = Tenant(name=slug, slug=slug, is_active=is_active)
    session.add(t)
    await session.flush()
    return t


async def _mk_user(
    session: AsyncSession,
    *,
    username: str,
    role: str,
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(username=username, password_hash="x", role=role, is_sso=is_sso, tenant_id=tenant_id)
    session.add(u)
    await session.flush()
    return u


class _Seed:
    default_id: int
    a_id: int
    b_id: int
    d_id: int
    c_inactive_id: int
    superadmin: AppUser
    local_admin: AppUser
    local_auditor: AppUser
    sso_admin: AppUser
    sso_auditor: AppUser


async def _seed(session: AsyncSession) -> _Seed:
    """Fünf Tenants (Default aus der Migration + A/B/D aktiv + C inaktiv) + fünf Konten,
    je eines pro Kombination, die das Vier-Wege-Modell unterscheiden muss:

    - `superadmin`: keine Zuweisungszeile nötig, instanzweit.
    - `local_admin`: `admin_tenant`-Grant NUR auf A.
    - `local_auditor`: `auditor_tenant`-Grant NUR auf B.
    - `sso_admin`: Heim-Tenant A (`tenant_id=A`, `role=="admin"`) UND ZUSÄTZLICH ein
      `admin_tenant`-Grant auf B (Haupttenant-SSO-Konto, vom Superadmin auf einen weiteren
      Kunden berechtigt) -- der non-vacuöse Beweis aus dem Brief: kann B beschreiben
      (Grant), aber NICHT D (kein Grant, kein Heim-Tenant).
    - `sso_auditor`: Heim-Tenant D (`tenant_id=D`, `role=="auditor"`) -- rein lesend, dort
      wie überall nie Schreibzugriff.
    """
    default = (await session.execute(select(Tenant).where(Tenant.slug == "default"))).scalar_one()
    a = await _mk_tenant(session, slug="am-tenant-a")
    b = await _mk_tenant(session, slug="am-tenant-b")
    d = await _mk_tenant(session, slug="am-tenant-d")
    c = await _mk_tenant(session, slug="am-tenant-c-inactive", is_active=False)
    assert a.id is not None and b.id is not None and d.id is not None and c.id is not None

    superadmin = await _mk_user(session, username="am-superadmin@local", role="superadmin")

    local_admin = await _mk_user(session, username="am-admin@local", role="admin")
    assert local_admin.id is not None
    session.add(AdminTenant(user_id=local_admin.id, tenant_id=a.id))

    local_auditor = await _mk_user(session, username="am-auditor@local", role="auditor")
    assert local_auditor.id is not None
    session.add(AuditorTenant(user_id=local_auditor.id, tenant_id=b.id))
    await session.flush()

    sso_admin = await _mk_user(
        session, username="am-sso-admin@a", role="admin", is_sso=True, tenant_id=a.id
    )
    assert sso_admin.id is not None
    session.add(AdminTenant(user_id=sso_admin.id, tenant_id=b.id))

    sso_auditor = await _mk_user(
        session, username="am-sso-auditor@d", role="auditor", is_sso=True, tenant_id=d.id
    )
    await session.flush()

    seed = _Seed()
    seed.default_id, seed.a_id, seed.b_id, seed.d_id, seed.c_inactive_id = (
        default.id,
        a.id,
        b.id,
        d.id,
        c.id,
    )
    seed.superadmin = superadmin
    seed.local_admin = local_admin
    seed.local_auditor = local_auditor
    seed.sso_admin = sso_admin
    seed.sso_auditor = sso_auditor
    return seed


# ---- allowed_tenant_ids: die effektive Lesemenge pro Kontoart -------------------------------- #


async def test_allowed_tenant_ids_superadmin_is_none(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.allowed_tenant_ids(session, seed.superadmin) is None


async def test_allowed_tenant_ids_local_admin_is_its_grant_set_not_none(
    session: AsyncSession,
) -> None:
    """Non-vakuoser Beweis der Kernänderung: NICHT mehr `None` (="alle")."""
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed.local_admin)
    assert ids == {seed.a_id}
    assert ids is not None


async def test_allowed_tenant_ids_local_auditor_is_its_grant_set(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed.local_auditor)
    assert ids == {seed.b_id}


async def test_allowed_tenant_ids_sso_admin_is_home_union_grant(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed.sso_admin)
    assert ids == {seed.a_id, seed.b_id}


async def test_allowed_tenant_ids_sso_auditor_is_home_tenant_only(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed.sso_auditor)
    assert ids == {seed.d_id}


# ---- admin_tenants / auditor_tenants: die effektiven Kapazitätsmengen ------------------------ #


async def test_admin_tenants_local_admin_is_grant_only(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.admin_tenants(session, seed.local_admin) == {seed.a_id}
    assert await tenant_repo.auditor_tenants(session, seed.local_admin) == set()


async def test_auditor_tenants_local_auditor_is_grant_only(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.auditor_tenants(session, seed.local_auditor) == {seed.b_id}
    assert await tenant_repo.admin_tenants(session, seed.local_auditor) == set()


async def test_admin_tenants_sso_admin_is_home_union_grant(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.admin_tenants(session, seed.sso_admin) == {seed.a_id, seed.b_id}
    assert await tenant_repo.auditor_tenants(session, seed.sso_admin) == set()


async def test_auditor_tenants_sso_auditor_is_home_only(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.auditor_tenants(session, seed.sso_auditor) == {seed.d_id}
    assert await tenant_repo.admin_tenants(session, seed.sso_auditor) == set()


# ---- is_allowed: table-driven read vs. write für alle vier Kontoarten ------------------------ #
#
# (Kontoart, Tenant-Schlüssel, write?, erwartet) -- `tid_key` referenziert `_Seed`-Attribute
# (`a_id`/`b_id`/`d_id`/`c_inactive_id`/`default_id`). Deckt pro Kontoart mindestens: einen
# erlaubten aktiven Tenant (read+write, sofern Admin-Kapazität), einen NICHT zugewiesenen
# Tenant, und -- wo zutreffend -- den inaktiven Tenant ab.

_CASES: list[tuple[str, str, bool, bool]] = [
    # -- Superadmin: alle aktiven, lesend wie schreibend; inaktiv immer False.
    ("superadmin", "a_id", False, True),
    ("superadmin", "b_id", False, True),
    ("superadmin", "d_id", False, True),
    ("superadmin", "default_id", False, True),
    ("superadmin", "a_id", True, True),
    ("superadmin", "b_id", True, True),
    ("superadmin", "c_inactive_id", False, False),
    ("superadmin", "c_inactive_id", True, False),
    # -- Lokaler Admin: NUR A, lesend UND schreibend; B/D/default/C verweigert.
    ("local_admin", "a_id", False, True),
    ("local_admin", "a_id", True, True),
    ("local_admin", "b_id", False, False),
    ("local_admin", "b_id", True, False),
    ("local_admin", "default_id", False, False),
    ("local_admin", "c_inactive_id", False, False),
    # -- Lokaler Auditor: NUR B, NUR lesend -- write ist auch für seinen eigenen Tenant False.
    ("local_auditor", "b_id", False, True),
    ("local_auditor", "b_id", True, False),
    ("local_auditor", "a_id", False, False),
    ("local_auditor", "c_inactive_id", False, False),
    # -- SSO-Admin: Heim A + Grant B, beide lesend UND schreibend; D (kein Grant/Heim) verweigert.
    ("sso_admin", "a_id", False, True),
    ("sso_admin", "a_id", True, True),
    ("sso_admin", "b_id", False, True),
    ("sso_admin", "b_id", True, True),
    ("sso_admin", "d_id", False, False),
    ("sso_admin", "d_id", True, False),
    # -- SSO-Auditor: NUR Heim D, NUR lesend.
    ("sso_auditor", "d_id", False, True),
    ("sso_auditor", "d_id", True, False),
    ("sso_auditor", "a_id", False, False),
    ("sso_auditor", "b_id", False, False),
]


@pytest.mark.parametrize("account_key,tid_key,write,expected", _CASES)
async def test_is_allowed_table_driven(
    session: AsyncSession, account_key: str, tid_key: str, write: bool, expected: bool
) -> None:
    seed = await _seed(session)
    user: AppUser = getattr(seed, account_key)
    tid: int = getattr(seed, tid_key)
    result = await tenant_repo.is_allowed(session, user, tid, write=write)
    assert result is expected, (
        f"{account_key} write={write} auf {tid_key}: erwartet {expected}, erhalten {result}"
    )


# ---- resolve_initial_tenant: pro Kontoart ----------------------------------------------------- #


async def test_resolve_initial_tenant_superadmin_is_default(session: AsyncSession) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed.superadmin)
    assert tid == seed.default_id


async def test_resolve_initial_tenant_local_admin_is_first_grant(session: AsyncSession) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed.local_admin)
    assert tid == seed.a_id


async def test_resolve_initial_tenant_local_auditor_is_first_grant(session: AsyncSession) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed.local_auditor)
    assert tid == seed.b_id


async def test_resolve_initial_tenant_sso_is_home_tenant(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.resolve_initial_tenant(session, seed.sso_admin) == seed.a_id
    assert await tenant_repo.resolve_initial_tenant(session, seed.sso_auditor) == seed.d_id


async def test_resolve_initial_tenant_unassigned_local_admin_is_none(
    session: AsyncSession,
) -> None:
    """Non-vakuoser Beweis: ohne JEDE `admin_tenant`-Zuweisung gibt es KEINEN stillen
    Fallback auf den Default-Tenant mehr (altes Drei-Wege-Modell-Verhalten)."""
    admin = await _mk_user(session, username="am-unassigned-admin@local", role="admin")
    assert await tenant_repo.resolve_initial_tenant(session, admin) is None
    assert await tenant_repo.allowed_tenant_ids(session, admin) == set()


# ---- Default-Deny bleibt erhalten (unbekannte Rolle) ------------------------------------------ #


async def test_unknown_role_stays_default_denied(session: AsyncSession) -> None:
    seed = await _seed(session)
    ghost = await _mk_user(session, username="am-ghost@local", role="totally-unknown-role")
    assert await tenant_repo.allowed_tenant_ids(session, ghost) == set()
    assert await tenant_repo.is_allowed(session, ghost, seed.a_id) is False
    assert await tenant_repo.is_allowed(session, ghost, seed.a_id, write=True) is False
    assert await tenant_repo.resolve_initial_tenant(session, ghost) is None
