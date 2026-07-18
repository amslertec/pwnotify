"""Task 4 der Console+Groups+Invite-Phase (KRONJUWEL): der Gruppen-Grant-Reconcile beim
SSO-Login (`assignment_group_repo.reconcile_group_grants`) -- er materialisiert
`source='group'`-Grants aus den Entra-Team-Mitgliedschaften eines Logins und MUSS zwei harte
Isolations-Invarianten wahren:

1. **Ein Kunden-homed Konto (oder `tenant_id is None`) erhält NIEMALS einen Gruppen-Grant** --
   die `is_provider_account`-Gate ist die ERSTE Zeile (fail-closed), vor jedem DB-Zugriff.
2. **Ein `source='manual'`-Grant wird NIE angefasst** (nicht konvertiert, nicht gelöscht) --
   ein manueller Superadmin-Grant gewinnt und bleibt `source='manual'`.
3. **Rollen-Flip-Sicherheit:** Gruppen-Grants existieren IMMER NUR in der Zieltabelle der
   AKTUELLEN Rolle -- ein Reconcile räumt bei jedem Aufruf zusätzlich verwaiste
   `source='group'`-Zeilen der ANDEREN Zieltabelle auf (kein dauerhafter Übergewähr bei
   admin<->auditor-Rollenwechsel eines Provider-Kontos).

Angriffs-orientiert: die Tests treiben `reconcile_group_grants` DIREKT an (wie
`test_assignment_bulk_cross_grant_lock.py` seine Route direkt treibt) und beweisen das
Verhalten an echtem Postgres.

NICHT-VAKUOS: `test_customer_a_homed_admin_never_group_granted_foreign_tenant` /
`test_null_home_account_never_group_granted` mappen ein Team, dessen Kunde OHNE die Gate
tatsächlich als Grant geschrieben würde -- erst die Gate lässt sie mit ZERO Zeilen enden.
`test_role_flip_stale_other_kind_group_grant_is_cleaned` bricht gegen den Vor-Fix-Stand: dort
würde die alte `admin_tenant(A, group)`-Zeile nach einer Demotion zu `auditor` erhalten
bleiben, weil der Reconcile vor dem Fix nur die Zieltabelle der NEUEN Rolle anfasst.

KORREKTUR (Sicherheitsreview, Minor-Finding): `test_manual_grant_precedence_*` prüft die
tragenden Kontrollen -- dass die manuelle Zeile `source='manual'` bleibt (nicht konvertiert)
und dass genau EINE Zeile für `(user, A)` existiert, sowie dass sie bei `groups=[]` persistiert.
Der `- manual_now`-Term in der Add-Menge ist dabei NICHT die tragende Schutzschicht -- er ist
redundante Defense-in-Depth. Die tragende Schicht ist der Composite-PK zusammen mit
`add_grant`s `on_conflict_do_nothing()`: eine bestehende manuelle Zeile blockiert das
Gruppen-INSERT bereits auf DB-Ebene, eine "Konvertierung" zu `source='group'` ist technisch
unmöglich (ON CONFLICT DO NOTHING lässt die bestehende Zeile unverändert). Der frühere Bericht
(`cg2-task-4-report.md`) behauptete fälschlich, ohne den Manual-Vorrang würde die Zeile "zu
`source='group'` konvertiert" -- siehe die Korrektur-Notiz dort.

Die savepoint-isolierte `session`-Fixture (`conftest.py`) IST der Aufräum-Mechanismus:
`add_grant`/`remove_grant` committen intern, unter der Fixture werden das Savepoints, der
äussere Rollback macht die Suite rückstandsfrei und zweimal hintereinander ausführbar -- exakt
wie bei der Route-Familie in `test_assignment_bulk_cross_grant_lock.py`, deshalb kein
manuelles `finally`-Aufräumen.
"""

from __future__ import annotations

import uuid

from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t4-recon-{uuid.uuid4().hex[:10]}"


def _entra() -> str:
    return f"grp-{uuid.uuid4().hex}"


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="T4 Recon Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = True,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=f"t4-recon-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def _mk_team(session: AsyncSession, tenant_ids: list[int], *, role: str = "admin") -> str:
    """Assignment-Group (Team) -> Kunden; gibt die (freie) `entra_group_id` zurück, die im
    `groups`-Claim eines Logins auftaucht."""
    entra = _entra()
    group = await assignment_group_repo.create(
        session, name="Team", entra_group_id=entra, role=role
    )
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, tenant_ids)
    return entra


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


async def _admin_row(session: AsyncSession, user_id: int, tenant_id: int) -> AdminTenant | None:
    return (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == user_id, AdminTenant.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


# ---- Provider admin gains team customers; empty groups removes group grants -------------- #


async def test_provider_admin_gains_team_customers_and_empty_removes_group_grants(
    session: AsyncSession,
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t1 = await _mk_team(session, [tenant_a.id])
    t2 = await _mk_team(session, [tenant_b.id])

    admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert admin.id is not None

    # groups=[T1] -> A (source='group'), NOT B.
    await assignment_group_repo.reconcile_group_grants(session, admin, [t1])
    rows = await _admin_rows(session, admin.id)
    assert {(r.tenant_id, r.source) for r in rows} == {(tenant_a.id, "group")}

    # groups=[T1, T2] -> also B.
    await assignment_group_repo.reconcile_group_grants(session, admin, [t1, t2])
    rows = await _admin_rows(session, admin.id)
    assert {(r.tenant_id, r.source) for r in rows} == {
        (tenant_a.id, "group"),
        (tenant_b.id, "group"),
    }

    # groups=[] -> BOTH group grants removed.
    await assignment_group_repo.reconcile_group_grants(session, admin, [])
    assert await _admin_rows(session, admin.id) == []


# ---- Manual precedence: manual row stays, is not converted/duplicated, and persists on [] - #


async def test_manual_grant_precedence_stays_and_persists(session: AsyncSession) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert admin.id is not None

    # Pre-existing MANUAL grant on A (an explicit superadmin action).
    await tenant_repo.add_grant(
        session, user_id=admin.id, tenant_id=tenant_a.id, kind="admin", source="manual"
    )

    # Reconcile groups=[T1] (which implies A): the A row STAYS source='manual' -- not
    # converted, not duplicated (exactly one row for (user, A)).
    await assignment_group_repo.reconcile_group_grants(session, admin, [t1])
    rows = await _admin_rows(session, admin.id)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_a.id
    assert rows[0].source == "manual"

    # Reconcile groups=[]: the manual A grant PERSISTS -- only group rows are reconciled away.
    await assignment_group_repo.reconcile_group_grants(session, admin, [])
    rows = await _admin_rows(session, admin.id)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_a.id
    assert rows[0].source == "manual"


# ---- THE INVARIANT: customer-homed / NULL-home account NEVER group-granted --------------- #


async def test_customer_a_homed_admin_never_group_granted_foreign_tenant(
    session: AsyncSession,
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    # Team T2 -> B; WITHOUT the gate, a customer-A-homed admin whose claim lists T2 would
    # gain admin_tenant(B, group). The gate must short-circuit that.
    t2 = await _mk_team(session, [tenant_b.id])

    customer_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    await assignment_group_repo.reconcile_group_grants(session, customer_admin, [t2])

    # ZERO admin/auditor grant rows -- neither the foreign B nor its own home A.
    assert await _admin_rows(session, customer_admin.id) == []
    assert await _auditor_rows(session, customer_admin.id) == []


async def test_null_home_account_never_group_granted(session: AsyncSession) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    null_home = await _mk_user(session, role="admin", tenant_id=None)
    assert null_home.id is not None

    await assignment_group_repo.reconcile_group_grants(session, null_home, [t1])

    assert await _admin_rows(session, null_home.id) == []
    assert await _auditor_rows(session, null_home.id) == []


# ---- Role drives kind: a provider auditor gets auditor_tenant, never admin_tenant -------- #


async def test_role_drives_kind_provider_auditor_gets_auditor_grant(
    session: AsyncSession,
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    auditor = await _mk_user(session, role="auditor", tenant_id=default.id)
    assert auditor.id is not None

    await assignment_group_repo.reconcile_group_grants(session, auditor, [t1])

    aud = await _auditor_rows(session, auditor.id)
    assert {(r.tenant_id, r.source) for r in aud} == {(tenant_a.id, "group")}
    # NEVER an admin_tenant row -- the role, not the caller, drives the grant kind.
    assert await _admin_rows(session, auditor.id) == []


# ---- Inactive tenant in a team is skipped silently (availability, not an error) ---------- #


async def test_inactive_tenant_in_team_is_skipped(session: AsyncSession) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_c = await _mk_tenant(session)  # created active so set_tenants accepts it
    assert tenant_c.id is not None
    t3 = await _mk_team(session, [tenant_c.id])
    # Deactivate C AFTER mapping the team to it -- the team still points at C, but C is no
    # longer active.
    await tenant_repo.update(session, tenant_c.id, is_active=False)

    admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert admin.id is not None

    # No grant written for the inactive tenant, and no error raised.
    await assignment_group_repo.reconcile_group_grants(session, admin, [t3])
    assert await _admin_row(session, admin.id, tenant_c.id) is None
    assert await _admin_rows(session, admin.id) == []


# ---- ROLE-FLIP SAFETY (Important finding, security review): stale other-kind group grant --- #
# ---- must not survive a role change; a manual grant of the other kind must never be moved -- #


async def test_role_flip_stale_other_kind_group_grant_is_cleaned(session: AsyncSession) -> None:
    """Non-vacuous against the pre-fix code: a provider admin holds `admin_tenant(A, group)`
    from Team T1. Entra then flips the account's role to `auditor` (a demotion, no group
    membership change). The NEXT reconcile (still groups=[T1]) must:
    - remove the now-stale `admin_tenant(A, group)` row entirely (the old role's kind is no
      longer `_grant_kind(user.role)` -- it is `other_kind`), and
    - materialize `auditor_tenant(A, group)` for the new role.

    Against the pre-fix code, the reconcile only ever touched `kind = _grant_kind(user.role)`
    (here `auditor` after the flip) and never looked at the OTHER kind's rows -- so the stale
    `admin_tenant(A, group)` row would survive untouched and this assertion would fail (the
    demoted account would silently retain write access to tenant A via `admin_tenants(user)`).
    """
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    account = await _mk_user(session, role="admin", tenant_id=default.id)
    assert account.id is not None

    # Login #1 as admin: gains admin_tenant(A, group).
    await assignment_group_repo.reconcile_group_grants(session, account, [t1])
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, account.id)} == {
        (tenant_a.id, "group")
    }
    assert await _auditor_rows(session, account.id) == []

    # Entra demotes the account to auditor (role flip, no group-membership change).
    account.role = "auditor"
    await session.flush()

    # Login #2 as auditor, same team membership.
    await assignment_group_repo.reconcile_group_grants(session, account, [t1])

    # The stale admin_tenant(A, group) row from the PREVIOUS role must be gone.
    assert await _admin_rows(session, account.id) == []
    # The new role's kind holds the group grant.
    assert {(r.tenant_id, r.source) for r in await _auditor_rows(session, account.id)} == {
        (tenant_a.id, "group")
    }


async def test_role_flip_cleanup_never_touches_manual_grant_of_other_kind(
    session: AsyncSession,
) -> None:
    """A provider account has a MANUAL auditor_tenant(A) grant (e.g. from a prior superadmin
    assignment) and role=admin. Reconciling as admin must clean only STALE GROUP rows of the
    other kind (auditor_tenant here) -- a manual row of the other kind is out of scope for this
    reconcile (governed by the assignment API / set_role instead) and must persist untouched."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t1 = await _mk_team(session, [tenant_a.id])

    account = await _mk_user(session, role="admin", tenant_id=default.id)
    assert account.id is not None

    # Pre-existing MANUAL auditor grant on A (the OTHER kind relative to the current role).
    await tenant_repo.add_grant(
        session, user_id=account.id, tenant_id=tenant_a.id, kind="auditor", source="manual"
    )

    await assignment_group_repo.reconcile_group_grants(session, account, [t1])

    # The manual auditor grant on A persists untouched.
    aud = await _auditor_rows(session, account.id)
    assert len(aud) == 1
    assert aud[0].tenant_id == tenant_a.id
    assert aud[0].source == "manual"
    # The current role's kind gained the expected group grant.
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, account.id)} == {
        (tenant_a.id, "group")
    }
