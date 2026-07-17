"""TDD für `tenant_repo`: die Autorisierungslogik "welcher AppUser darf welchen Tenant
sehen/aktivieren" -- die Sicherheitsgrenze von Phase 4a Task 2.

Läuft auf der gewöhnlichen, savepoint-isolierten `session`-Fixture (Owner-Session, kein
Rollenwechsel): `tenant_repo` führt seine Queries direkt auf der übergebenen `AsyncSession`
aus und öffnet -- anders als `tenant_scoped_session` -- keine eigene Verbindung. Ein echtes
Commit-über-eine-zweite-Connection-Setup (wie in test_isolation_attack.py) ist daher nicht
nötig; die Savepoint-Rücksetzung aus conftest.py räumt jeden Testlauf rückstandsfrei auf.
Der Default-Tenant (`slug='default'`) existiert bereits aus der Migration und wird nur
gelesen, nie angelegt.

Access-Modell/Superadmin-Phase, Task 2: der lokale Admin ist NICHT mehr instanzweit --
der Admin-Abschnitt unten (`_seed`, "Lokaler Admin") wurde entsprechend auf das
`admin_tenant`-Grant-Modell umgestellt. Der Superadmin (instanzweit, wie der alte lokale
Admin es war) sowie das table-driven Vier-Wege-Modell (inkl. write-vs-read und SSO mit
Zusatz-Grant) leben in `test_tenant_repo_access_model.py`."""

from __future__ import annotations

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
    role: str = "admin",
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=username,
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def _seed(session: AsyncSession) -> dict[str, object]:
    """Default-Tenant (aus Migration) + zwei aktive Tenants A/B + ein inaktiver Tenant C +
    vier Konten: lokaler Admin (NUR Tenant A per `admin_tenant` zugewiesen -- Access-Modell-
    Task-2-Verhalten, nicht mehr instanzweit), lokaler Auditor (nur A zugewiesen), SSO-Konto
    (an B gebunden), lokaler Auditor ohne jede Zuweisung."""
    default = (await session.execute(select(Tenant).where(Tenant.slug == "default"))).scalar_one()
    a = await _mk_tenant(session, slug="tenant-a")
    b = await _mk_tenant(session, slug="tenant-b")
    c = await _mk_tenant(session, slug="tenant-c", is_active=False)

    admin = await _mk_user(session, username="admin@local")
    assert admin.id is not None
    assert a.id is not None
    session.add(AdminTenant(user_id=admin.id, tenant_id=a.id))
    auditor_a = await _mk_user(session, username="auditor-a@local", role="auditor")
    assert auditor_a.id is not None
    session.add(AuditorTenant(user_id=auditor_a.id, tenant_id=a.id))
    await session.flush()
    sso_b = await _mk_user(session, username="sso@b", role="admin", is_sso=True, tenant_id=b.id)
    unassigned_auditor = await _mk_user(session, username="auditor-none@local", role="auditor")

    return {
        "default": default,
        "a": a,
        "b": b,
        "c": c,
        "admin": admin,
        "auditor_a": auditor_a,
        "sso_b": sso_b,
        "unassigned_auditor": unassigned_auditor,
    }


# ---- list_active / get / default_tenant / get_by_entra_tid ---------------------------------- #


async def test_list_active_excludes_inactive(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = {t.id for t in await tenant_repo.list_active(session)}
    assert seed["a"].id in ids  # type: ignore[attr-defined]
    assert seed["b"].id in ids  # type: ignore[attr-defined]
    assert seed["c"].id not in ids  # type: ignore[attr-defined]


async def test_get_returns_tenant_or_none(session: AsyncSession) -> None:
    seed = await _seed(session)
    a = seed["a"]
    found = await tenant_repo.get(session, a.id)  # type: ignore[attr-defined]
    assert found is not None and found.id == a.id  # type: ignore[attr-defined]
    assert await tenant_repo.get(session, 999_999) is None


async def test_default_tenant_returns_seeded_default(session: AsyncSession) -> None:
    seed = await _seed(session)
    d = await tenant_repo.default_tenant(session)
    assert d.slug == "default"
    assert d.id == seed["default"].id  # type: ignore[attr-defined]


async def test_get_by_entra_tid_only_matches_active(session: AsyncSession) -> None:
    active = Tenant(
        name="entra-active", slug="entra-active", entra_tenant_id="tid-active", is_active=True
    )
    inactive = Tenant(
        name="entra-inactive",
        slug="entra-inactive",
        entra_tenant_id="tid-inactive",
        is_active=False,
    )
    session.add_all([active, inactive])
    await session.flush()

    found = await tenant_repo.get_by_entra_tid(session, "tid-active")
    assert found is not None and found.id == active.id
    assert await tenant_repo.get_by_entra_tid(session, "tid-inactive") is None
    assert await tenant_repo.get_by_entra_tid(session, "unknown-tid") is None


# ---- Lokaler Admin: NUR seine `admin_tenant`-Zuweisung (Access-Modell-Task-2) ----------------- #
#
# Verhaltensänderung ggü. dem alten Drei-Wege-Modell: der lokale Admin ist NICHT mehr
# instanzweit ("alle aktiven Tenants"/`None`) -- er ist jetzt exakt wie der lokale Auditor
# auf seine Zuweisungen (hier: nur Tenant A) beschränkt. Das Superadmin-Pendant zum alten
# "sieht alles" lebt in `test_tenant_repo_access_model.py`.


async def test_local_admin_allowed_tenant_ids_is_its_grant_set(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed["admin"])  # type: ignore[arg-type]
    assert ids == {seed["a"].id}, "Lokaler Admin ist NICHT mehr instanzweit (None)"  # type: ignore


async def test_local_admin_is_allowed_only_granted_tenant(session: AsyncSession) -> None:
    seed = await _seed(session)
    admin = seed["admin"]
    assert await tenant_repo.is_allowed(session, admin, seed["a"].id) is True  # type: ignore
    assert await tenant_repo.is_allowed(session, admin, seed["b"].id) is False  # type: ignore
    assert await tenant_repo.is_allowed(session, admin, seed["default"].id) is False  # type: ignore
    assert await tenant_repo.is_allowed(session, admin, seed["c"].id) is False  # type: ignore
    # Schreibzugriff folgt derselben Zuweisung (write=True erfordert admin_tenants(user)).
    assert await tenant_repo.is_allowed(session, admin, seed["a"].id, write=True) is True  # type: ignore
    assert await tenant_repo.is_allowed(session, admin, seed["b"].id, write=True) is False  # type: ignore


async def test_local_admin_resolve_initial_tenant_is_first_granted(session: AsyncSession) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed["admin"])  # type: ignore[arg-type]
    assert tid == seed["a"].id, "Kein stiller Fallback auf den Default-Tenant mehr"  # type: ignore


# ---- Lokaler Auditor: nur zugewiesene + aktive Tenants --------------------------------------- #


async def test_local_auditor_allowed_tenant_ids_is_assigned_set(session: AsyncSession) -> None:
    seed = await _seed(session)
    ids = await tenant_repo.allowed_tenant_ids(session, seed["auditor_a"])  # type: ignore[arg-type]
    assert ids == {seed["a"].id}  # type: ignore[attr-defined]


async def test_local_auditor_is_allowed_only_assigned(session: AsyncSession) -> None:
    seed = await _seed(session)
    auditor = seed["auditor_a"]
    assert await tenant_repo.is_allowed(session, auditor, seed["a"].id) is True  # type: ignore
    assert await tenant_repo.is_allowed(session, auditor, seed["b"].id) is False  # type: ignore
    assert await tenant_repo.is_allowed(session, auditor, seed["c"].id) is False  # type: ignore


async def test_local_auditor_resolve_initial_tenant_is_first_assigned(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed["auditor_a"])  # type: ignore
    assert tid == seed["a"].id  # type: ignore[attr-defined]


async def test_local_auditor_deactivated_assignment_is_excluded(session: AsyncSession) -> None:
    """Ein Tenant ist per auditor_tenant zugewiesen, wird aber danach deaktiviert -- darf
    weder in allowed_tenant_ids auftauchen noch is_allowed/resolve_initial_tenant liefern."""
    t = await _mk_tenant(session, slug="tenant-d", is_active=True)
    auditor = await _mk_user(session, username="auditor-d@local", role="auditor")
    assert auditor.id is not None and t.id is not None
    session.add(AuditorTenant(user_id=auditor.id, tenant_id=t.id))
    await session.flush()

    t.is_active = False
    await session.flush()

    assert await tenant_repo.allowed_tenant_ids(session, auditor) == set()
    assert await tenant_repo.is_allowed(session, auditor, t.id) is False
    assert await tenant_repo.resolve_initial_tenant(session, auditor) is None


# ---- Edge case: lokaler Auditor ohne jede Zuweisung ------------------------------------------ #


async def test_local_auditor_unassigned_gets_nothing(session: AsyncSession) -> None:
    seed = await _seed(session)
    unassigned = seed["unassigned_auditor"]
    assert await tenant_repo.allowed_tenant_ids(session, unassigned) == set()  # type: ignore
    assert await tenant_repo.is_allowed(session, unassigned, seed["a"].id) is False  # type: ignore
    assert await tenant_repo.resolve_initial_tenant(session, unassigned) is None  # type: ignore


# ---- SSO-Konto: genau sein AppUser.tenant_id -------------------------------------------------- #


async def test_sso_account_allowed_tenant_ids_is_its_own_tenant(session: AsyncSession) -> None:
    seed = await _seed(session)
    assert await tenant_repo.allowed_tenant_ids(session, seed["sso_b"]) == {  # type: ignore
        seed["b"].id  # type: ignore[attr-defined]
    }


async def test_sso_account_is_allowed_only_its_own_active_tenant(session: AsyncSession) -> None:
    seed = await _seed(session)
    sso = seed["sso_b"]
    assert await tenant_repo.is_allowed(session, sso, seed["b"].id) is True  # type: ignore
    assert await tenant_repo.is_allowed(session, sso, seed["a"].id) is False  # type: ignore
    assert await tenant_repo.is_allowed(session, sso, seed["c"].id) is False  # type: ignore


async def test_sso_account_resolve_initial_tenant_is_bound_tenant(session: AsyncSession) -> None:
    seed = await _seed(session)
    tid = await tenant_repo.resolve_initial_tenant(session, seed["sso_b"])  # type: ignore
    assert tid == seed["b"].id  # type: ignore[attr-defined]


async def test_sso_account_bound_to_inactive_tenant_is_denied(session: AsyncSession) -> None:
    """allowed_tenant_ids liefert für SSO bewusst ungefiltert die Bindung -- is_allowed, das
    autoritative Gate, muss den inaktiven Tenant trotzdem ablehnen."""
    seed = await _seed(session)
    sso_c = await _mk_user(
        session,
        username="sso@c",
        role="admin",
        is_sso=True,
        tenant_id=seed["c"].id,  # type: ignore
    )
    assert await tenant_repo.is_allowed(session, sso_c, seed["c"].id) is False  # type: ignore
    assert await tenant_repo.resolve_initial_tenant(session, sso_c) == seed["c"].id  # type: ignore


# ---- Edge case: SSO-Konto ohne tenant_id (sollte nach Login nicht vorkommen) ------------------ #


async def test_sso_account_without_tenant_id_is_defensively_denied(
    session: AsyncSession,
) -> None:
    sso_none = await _mk_user(
        session, username="sso@none", role="admin", is_sso=True, tenant_id=None
    )
    assert await tenant_repo.allowed_tenant_ids(session, sso_none) == set()
    assert await tenant_repo.is_allowed(session, sso_none, 1) is False
    assert await tenant_repo.resolve_initial_tenant(session, sso_none) is None


# ---- Default-Deny bei unerwarteter Rolle ------------------------------------------------------ #


async def test_unknown_role_is_default_denied(session: AsyncSession) -> None:
    seed = await _seed(session)
    ghost = await _mk_user(session, username="ghost@local", role="totally-unknown-role")
    assert await tenant_repo.allowed_tenant_ids(session, ghost) == set()
    assert await tenant_repo.is_allowed(session, ghost, seed["a"].id) is False  # type: ignore
    assert await tenant_repo.resolve_initial_tenant(session, ghost) is None
