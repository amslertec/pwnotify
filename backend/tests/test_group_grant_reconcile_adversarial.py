"""Console+Groups+Invite-Phase (KRONJUWEL): der Gruppen-Grant-Reconcile
(`assignment_group_repo.reconcile_group_grants`) -- er materialisiert `source='group'`-Grants
aus den Entra-Team-Mitgliedschaften eines Logins und MUSS drei harte Invarianten wahren:

1. **Ein Kunden-homed Konto (oder `tenant_id is None`) erhält NIEMALS einen Gruppen-Grant** --
   die `is_provider_account`-Gate ist die ERSTE Zeile (fail-closed), vor jedem DB-Zugriff.
2. **Ein `source='manual'`-Grant wird NIE angefasst** (nicht konvertiert, nicht gelöscht) --
   ein manueller Superadmin-Grant gewinnt und bleibt `source='manual'`.
3. **Die ROLLE DES TEAMS entscheidet die Zieltabelle** eines Kunden-Grants (nicht die globale
   Rolle des einloggenden Kontos): Admin-Team -> `admin_tenant`, Auditor-Team ->
   `auditor_tenant`, **Admin gewinnt**, wenn ein Kunde von beiden Sorten Team abgebildet wird.
   Jede Zieltabelle wird gegen ihre EIGENE Wunschmenge abgeglichen -- dieser per-Tabelle-
   Abgleich IST die vollständige Rollen-Flip-Bereinigung.

Angriffs-orientiert: die Tests treiben `reconcile_group_grants` DIREKT an und assertieren
DIREKT auf den `admin_tenant`/`auditor_tenant`-Tabellen an echtem Postgres (RLS partizipiert).
Jeder Test würde ROT, wäre die Gate, der Manual-Vorrang oder die Admin-gewinnt-Faltung defekt.

Die savepoint-isolierte `session`-Fixture (`conftest.py`) IST der Aufräum-Mechanismus:
`add_grant`/`remove_grant` committen intern, unter der Fixture werden das Savepoints, der
äussere Rollback macht die Suite rückstandsfrei und wiederholbar -- kein manuelles Aufräumen.
"""

from __future__ import annotations

import uuid

from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"gr-recon-{uuid.uuid4().hex[:10]}"


def _entra() -> str:
    return f"grp-{uuid.uuid4().hex}"


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="Grp Recon Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


async def _mk_user(
    session: AsyncSession,
    *,
    role: str = "admin",
    is_sso: bool = True,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=f"gr-recon-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def _mk_team(session: AsyncSession, tenant_ids: list[int], *, role: str = "admin") -> str:
    """Assignment-Group (Team) -> Kunden mit einer ROLLE; gibt die (freie) `entra_group_id`
    zurück, die im `groups`-Claim eines Logins auftaucht."""
    entra = _entra()
    group = await assignment_group_repo.create(
        session, name="Team", entra_group_id=entra, role=role
    )
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, tenant_ids)
    return entra


async def _set_team_role(session: AsyncSession, entra: str, role: str) -> None:
    """Rolle eines bestehenden Teams umsetzen (simuliert eine Superadmin-Umstufung des Teams)."""
    group = await assignment_group_repo.get_by_entra_group_id(session, entra)
    assert group is not None and group.id is not None
    await assignment_group_repo.update(session, group.id, name=group.name, role=role)


async def _admin_rows(session: AsyncSession, user_id: int) -> list[AdminTenant]:
    return list(
        (await session.execute(select(AdminTenant).where(AdminTenant.user_id == user_id))).scalars()
    )


async def _auditor_rows(session: AsyncSession, user_id: int) -> list[AuditorTenant]:
    return list(
        (
            await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == user_id))
        ).scalars()
    )


def _pairs(rows: list[AdminTenant] | list[AuditorTenant]) -> set[tuple[int, str]]:
    return {(r.tenant_id, r.source) for r in rows}


# ---- Per-team role drives the grant KIND -- no cross-kind bleed ------------------------------ #


async def test_per_team_role_drives_grant_kind_no_cross_kind(session: AsyncSession) -> None:
    """Admin-Team T1 -> A, Auditor-Team T2 -> B. Ein Login in beiden Teams landet A in
    `admin_tenant` und B in `auditor_tenant` -- und in KEINER Kreuz-Tabelle. Die Rolle des
    TEAMS entscheidet, nicht die (hier admin) Rolle des einloggenden Kontos."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    g1 = await _mk_team(session, [tenant_a.id], role="admin")
    g2 = await _mk_team(session, [tenant_b.id], role="auditor")

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider.id is not None

    await assignment_group_repo.reconcile_group_grants(session, provider, [g1, g2])

    assert _pairs(await _admin_rows(session, provider.id)) == {(tenant_a.id, "group")}
    assert _pairs(await _auditor_rows(session, provider.id)) == {(tenant_b.id, "group")}


# ---- Admin wins when a customer is mapped by BOTH an admin- and an auditor-team -------------- #


async def test_admin_wins_when_customer_mapped_by_admin_and_auditor_team(
    session: AsyncSession,
) -> None:
    """C ist von T1(admin) UND T3(auditor) abgebildet. Admin gewinnt: C landet in
    `admin_tenant` (Schreiben), ZERO `auditor_tenant(C)`. Würde die Faltung Auditor gewinnen
    lassen (oder beide schreiben), verlöre C entweder Schreibzugriff oder bekäme eine
    widersprüchliche Doppelkapazität -- beides ROT hier."""
    default = await tenant_repo.default_tenant(session)
    tenant_c = await _mk_tenant(session)
    assert tenant_c.id is not None
    g1 = await _mk_team(session, [tenant_c.id], role="admin")
    g3 = await _mk_team(session, [tenant_c.id], role="auditor")

    provider = await _mk_user(session, role="auditor", tenant_id=default.id)  # home role irrelevant
    assert provider.id is not None

    await assignment_group_repo.reconcile_group_grants(session, provider, [g1, g3])

    assert _pairs(await _admin_rows(session, provider.id)) == {(tenant_c.id, "group")}
    assert await _auditor_rows(session, provider.id) == []


# ---- Role flip on the TEAM revokes the stale kind and adds the new one ----------------------- #


async def test_role_flip_auditor_team_to_admin_revokes_auditor_adds_admin(
    session: AsyncSession,
) -> None:
    """B kommt zunächst über ein AUDITOR-Team T2 -> `auditor_tenant(B, group)`. Der Superadmin
    stuft T2 auf `admin` um; der nächste Reconcile (dieselbe Team-Mitgliedschaft) muss die
    verwaiste `auditor_tenant(B, group)`-Zeile ENTFERNEN und `admin_tenant(B, group)` anlegen.
    Der per-Tabelle-Abgleich IST diese Bereinigung: B fällt aus `desired_auditor` und erscheint
    in `desired_admin`. Bliebe die alte Zeile stehen, behielte B stillen Nur-Lese-Zugriff der
    alten Kapazität zusätzlich zum neuen Schreibzugriff -> ROT."""
    default = await tenant_repo.default_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_b.id is not None
    g2 = await _mk_team(session, [tenant_b.id], role="auditor")

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider.id is not None

    await assignment_group_repo.reconcile_group_grants(session, provider, [g2])
    assert _pairs(await _auditor_rows(session, provider.id)) == {(tenant_b.id, "group")}
    assert await _admin_rows(session, provider.id) == []

    # Superadmin promotes the team auditor -> admin (no membership change).
    await _set_team_role(session, g2, "admin")

    await assignment_group_repo.reconcile_group_grants(session, provider, [g2])
    assert _pairs(await _admin_rows(session, provider.id)) == {(tenant_b.id, "group")}
    assert await _auditor_rows(session, provider.id) == []


# ---- Leaving all teams removes every group row from BOTH tables; manual persists ------------- #


async def test_leave_all_teams_removes_group_from_both_tables_manual_persists(
    session: AsyncSession,
) -> None:
    """Nach Grants in BEIDEN Tabellen (admin über T1, auditor über T2) plus einer manuellen
    Admin-Zeile auf C: `reconcile([])` entfernt ALLE `source='group'`-Zeilen aus beiden
    Tabellen, während die manuelle Zeile UNBERÜHRT bleibt (Manual-Vorrang gilt tabellenweit)."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    tenant_c = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None and tenant_c.id is not None
    g1 = await _mk_team(session, [tenant_a.id], role="admin")
    g2 = await _mk_team(session, [tenant_b.id], role="auditor")

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider.id is not None

    # A pre-existing MANUAL admin grant on C (an explicit superadmin action).
    await tenant_repo.add_grant(
        session, user_id=provider.id, tenant_id=tenant_c.id, kind="admin", source="manual"
    )

    await assignment_group_repo.reconcile_group_grants(session, provider, [g1, g2])
    assert _pairs(await _admin_rows(session, provider.id)) == {
        (tenant_a.id, "group"),
        (tenant_c.id, "manual"),
    }
    assert _pairs(await _auditor_rows(session, provider.id)) == {(tenant_b.id, "group")}

    # Leave every team: all source='group' rows vanish from BOTH tables; manual C survives.
    await assignment_group_repo.reconcile_group_grants(session, provider, [])
    assert _pairs(await _admin_rows(session, provider.id)) == {(tenant_c.id, "manual")}
    assert await _auditor_rows(session, provider.id) == []


# ---- Manual precedence PER TABLE: manual auditor(A) stays; admin-team adds admin(A) ---------- #


async def test_manual_precedence_per_table_no_dup_no_convert(session: AsyncSession) -> None:
    """P hält eine manuelle `auditor_tenant(A, manual)`-Zeile. Ein ADMIN-Team T1 bildet A ab.
    Der Reconcile fügt `admin_tenant(A, group)` an (ANDERE Tabelle) -- die manuelle Auditor-
    Zeile bleibt EXAKT `source='manual'` (nicht konvertiert, keine Dublette). Ein Bug, der die
    manuelle Zeile konvertierte oder eine zweite anlegte, wäre ROT."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g1 = await _mk_team(session, [tenant_a.id], role="admin")

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider.id is not None

    await tenant_repo.add_grant(
        session, user_id=provider.id, tenant_id=tenant_a.id, kind="auditor", source="manual"
    )

    await assignment_group_repo.reconcile_group_grants(session, provider, [g1])

    # Manual auditor row untouched (exactly one, still source='manual').
    assert _pairs(await _auditor_rows(session, provider.id)) == {(tenant_a.id, "manual")}
    # Admin-team materialized the admin grant in the OTHER table.
    assert _pairs(await _admin_rows(session, provider.id)) == {(tenant_a.id, "group")}


# ---- THE GATE: customer-homed AND null-home accounts are hard no-ops ------------------------- #


async def test_customer_homed_account_admin_team_maps_foreign_tenant_is_noop(
    session: AsyncSession,
) -> None:
    """Ein KUNDEN-A-homed Konto, dessen `groups` ein Admin-Team auf den fremden Kunden B
    mappen. OHNE die Gate würde `admin_tenant(B, group)` geschrieben -- die Gate (erste Zeile,
    fail-closed) macht den Reconkile zum No-Op: ZERO Zeilen in beiden Tabellen."""
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    g_admin = await _mk_team(session, [tenant_b.id], role="admin")

    customer_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    await assignment_group_repo.reconcile_group_grants(session, customer_admin, [g_admin])

    assert await _admin_rows(session, customer_admin.id) == []
    assert await _auditor_rows(session, customer_admin.id) == []


async def test_null_home_account_admin_team_is_noop(session: AsyncSession) -> None:
    """Ein Konto mit `tenant_id is None` (Heimat nie aufgelöst) darf ebenfalls NIE einen
    Gruppen-Grant erhalten -- default-deny in `is_provider_account`. ZERO Zeilen."""
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g_admin = await _mk_team(session, [tenant_a.id], role="admin")

    null_home = await _mk_user(session, role="admin", tenant_id=None)
    assert null_home.id is not None

    await assignment_group_repo.reconcile_group_grants(session, null_home, [g_admin])

    assert await _admin_rows(session, null_home.id) == []
    assert await _auditor_rows(session, null_home.id) == []


# ---- Inactive tenant in a team is skipped silently (availability, not an error) -------------- #


async def test_inactive_tenant_in_team_is_skipped(session: AsyncSession) -> None:
    """Ein Team bildet einen zwischenzeitlich DEAKTIVIERTEN Kunden ab -- der Reconcile
    überspringt ihn beim Anlegen still (kein Fehler, keine Zeile)."""
    default = await tenant_repo.default_tenant(session)
    tenant_c = await _mk_tenant(session)  # created active so set_tenants accepts it
    assert tenant_c.id is not None
    g3 = await _mk_team(session, [tenant_c.id], role="admin")
    await tenant_repo.update(session, tenant_c.id, is_active=False)

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider.id is not None

    await assignment_group_repo.reconcile_group_grants(session, provider, [g3])
    assert await _admin_rows(session, provider.id) == []
    assert await _auditor_rows(session, provider.id) == []
