"""Task 3 der Multi-Tenant-Phase (KRONJUWEL): der proaktive Entra-Gruppen-Sync
(`services/group_sync.sync_group`). Er bringt den lokalen Mitglieder-Snapshot einer
`AssignmentGroup` auf Graph-Stand und materialisiert daraus Kunden-Zugriffe -- ABER
ausschliesslich über die sicherheitsgeprüfte `reconcile_group_grants`. Es gibt keinen eigenen
Grant-Schreibpfad.

Angriffs-orientiert (wie `test_group_grant_reconcile_adversarial.py`): die Tests treiben
`sync_group` mit einem gefälschten `get_group_members` an und behaupten DIREKT auf den
`admin_tenant`/`auditor_tenant`-Grant-Tabellen an echtem Postgres.

Die harte Isolations-Invariante: ein kunden-homed oder `tenant_id is None`-Snapshot-Mitglied
erhält NIEMALS einen Grant -- die `is_provider_account`-Gate in `reconcile_group_grants`
schluckt es, obwohl der Sync ohne die Gate einen Fremd-Grant schriebe (non-vakuos).

Die savepoint-isolierte `session`-Fixture (`conftest.py`) räumt auf: `add_grant`/`remove_grant`
committen intern, unter der Fixture sind das Savepoints, der äussere Rollback macht die Suite
rückstandsfrei -- kein manuelles `finally`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.errors import GraphError
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import assignment_group_member_repo as member_repo
from app.repositories import assignment_group_repo, tenant_repo
from app.services import group_sync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ---- Seeding helpers (mirrors test_group_grant_reconcile_adversarial.py) ------------------ #


def _slug() -> str:
    return f"t3-sync-{uuid.uuid4().hex[:10]}"


def _entra() -> str:
    return f"grp-{uuid.uuid4().hex}"


def _member(upn: str, *, entra_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    return {
        "id": entra_id or f"eid-{uuid.uuid4().hex}",
        "userPrincipalName": upn,
        "displayName": name or upn,
        "mail": upn,
    }


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="T3 Sync Tenant", slug=_slug())


async def _mk_user(
    session: AsyncSession, *, upn: str, role: str, tenant_id: int | None, is_sso: bool = True
) -> AppUser:
    """Konto, dessen `username` EXAKT der UPN entspricht -- der Sync matcht case-sensitive."""
    u = AppUser(username=upn, password_hash="x", role=role, is_sso=is_sso, tenant_id=tenant_id)
    session.add(u)
    await session.flush()
    return u


async def _mk_team(
    session: AsyncSession, tenant_ids: list[int], *, role: str = "admin"
) -> tuple[int, str]:
    """Assignment-Group (Team) -> Kunden; gibt `(group_id, entra_group_id)` zurück."""
    entra = _entra()
    group = await assignment_group_repo.create(
        session, name="Team", entra_group_id=entra, role=role
    )
    assert group.id is not None
    await assignment_group_repo.set_tenants(session, group.id, tenant_ids)
    return group.id, entra


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


class _FakeGraph:
    """Ersetzt `GraphClient` im Sync -- liefert die vorgegebenen Mitglieder je `entra_group_id`,
    oder wirft (Graph-Fehler-Pfad)."""

    def __init__(
        self,
        members_by_entra: dict[str, list[dict[str, Any]]],
        *,
        raises: Exception | None = None,
    ) -> None:
        self._members = members_by_entra
        self._raises = raises

    async def get_group_members(self, entra_group_id: str) -> list[dict[str, Any]]:
        if self._raises is not None:
            raise self._raises
        return list(self._members.get(entra_group_id, []))


def _patch_graph(
    monkeypatch: pytest.MonkeyPatch,
    members_by_entra: dict[str, list[dict[str, Any]]],
    *,
    raises: Exception | None = None,
) -> None:
    monkeypatch.setattr(
        group_sync, "GraphClient", lambda _cfg: _FakeGraph(members_by_entra, raises=raises)
    )


# ---- Provider member materialized: grant for its team's tenant, none else ---------------- #


async def test_provider_member_materialized_for_team_tenant_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    g1_id, g1 = await _mk_team(session, [tenant_a.id])
    _g2_id, g2 = await _mk_team(session, [tenant_b.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    admin = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert admin.id is not None

    _patch_graph(monkeypatch, {g1: [_member(upn)], g2: []})
    result = await group_sync.sync_group(session, {}, g1_id)

    # admin_tenant(A, source='group') and NOTHING for B.
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, admin.id)} == {
        (tenant_a.id, "group")
    }
    assert await _auditor_rows(session, admin.id) == []
    assert result == {"member_count": 1, "materialized": 1, "added": 1, "removed": 0}


# ---- THE INVARIANT: customer-homed / NULL-home member NEVER group-granted ----------------- #


async def test_customer_homed_member_zero_grant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    # Team T2 -> B. WITHOUT the gate, a customer-A-homed admin whose UPN is in T2 would gain
    # admin_tenant(B, group). The gate must short-circuit that to a no-op (non-vacuous).
    g2_id, g2 = await _mk_team(session, [tenant_b.id])

    upn = f"customer-a-{uuid.uuid4().hex}@a.example"
    customer_admin = await _mk_user(session, upn=upn, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    _patch_graph(monkeypatch, {g2: [_member(upn)]})
    result = await group_sync.sync_group(session, {}, g2_id)

    # ZERO grant rows -- neither the foreign B nor its own home A. Snapshot still holds it.
    assert await _admin_rows(session, customer_admin.id) == []
    assert await _auditor_rows(session, customer_admin.id) == []
    assert result["materialized"] == 0
    assert result["member_count"] == 1
    assert upn in await member_repo.upns_for_group(session, g2_id)


async def test_null_home_member_zero_grant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g1_id, g1 = await _mk_team(session, [tenant_a.id])

    upn = f"nullhome-{uuid.uuid4().hex}@x.example"
    null_home = await _mk_user(session, upn=upn, role="admin", tenant_id=None)
    assert null_home.id is not None

    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    result = await group_sync.sync_group(session, {}, g1_id)

    assert await _admin_rows(session, null_home.id) == []
    assert await _auditor_rows(session, null_home.id) == []
    assert result["materialized"] == 0


# ---- Team-leave revokes the source='group' grant; a manual grant persists ----------------- #


async def test_team_leave_revokes_group_grant_manual_persists(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider admin materialized into A via T1. A separate MANUAL admin_tenant(B) exists.
    Removing the UPN from T1's fetched members and re-syncing revokes the source='group' A
    grant, while the manual B grant persists untouched.

    (Deviation from the brief's literal "manual on A": a manual and a group row cannot coexist
    on the SAME (user, tenant, kind) -- the composite PK + manual precedence means no group row
    ever forms on A, making a "group removed" assertion vacuous. Putting the manual grant on a
    DISTINCT tenant B makes both claims -- group revoked, manual survives -- genuinely testable.)
    """
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    g1_id, g1 = await _mk_team(session, [tenant_a.id])

    upn = f"provider-{uuid.uuid4().hex}@provider.example"
    admin = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert admin.id is not None

    # Pre-existing MANUAL grant on B (an explicit superadmin action, unrelated to any team).
    await tenant_repo.add_grant(
        session, user_id=admin.id, tenant_id=tenant_b.id, kind="admin", source="manual"
    )

    # Sync 1: member present in T1 -> group A materialized; manual B untouched.
    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, admin.id)} == {
        (tenant_a.id, "group"),
        (tenant_b.id, "manual"),
    }

    # Sync 2: member REMOVED from T1 -> its team set no longer contains g1 -> group A revoked;
    # the manual B grant persists.
    _patch_graph(monkeypatch, {g1: []})
    result = await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, admin.id)} == {
        (tenant_b.id, "manual")
    }
    assert result["removed"] == 1
    # The removed member is materialized (reconciled via the OLD/NEW union) even though it is
    # gone from the group -- that is how its stale grant gets revoked.
    assert result["materialized"] == 1


# ---- Unmatched member stays snapshot-only: not materialized, no grant --------------------- #


async def test_unmatched_member_snapshot_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g1_id, g1 = await _mk_team(session, [tenant_a.id])

    upn = f"nobody-{uuid.uuid4().hex}@x.example"  # no app_user with this username
    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    result = await group_sync.sync_group(session, {}, g1_id)

    # Present in the snapshot, but not counted as materialized and no grant rows anywhere.
    assert upn in await member_repo.upns_for_group(session, g1_id)
    assert result == {"member_count": 1, "materialized": 0, "added": 1, "removed": 0}
    admin_any = (
        (await session.execute(select(AdminTenant).where(AdminTenant.tenant_id == tenant_a.id)))
        .scalars()
        .all()
    )
    assert list(admin_any) == []


# ---- Role drives the grant kind through the sync path ------------------------------------- #


async def test_role_drives_kind_provider_auditor_gets_auditor_grant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g1_id, g1 = await _mk_team(session, [tenant_a.id])

    upn = f"provider-auditor-{uuid.uuid4().hex}@provider.example"
    auditor = await _mk_user(session, upn=upn, role="auditor", tenant_id=default.id)
    assert auditor.id is not None

    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)

    # auditor_tenant(A, group), NEVER an admin_tenant row -- the role drives the kind.
    assert {(r.tenant_id, r.source) for r in await _auditor_rows(session, auditor.id)} == {
        (tenant_a.id, "group")
    }
    assert await _admin_rows(session, auditor.id) == []


# ---- Graph failure -> typed sync_failed error, snapshot untouched (never a 500) ----------- #


async def test_graph_error_raises_sync_failed_and_leaves_snapshot(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    g1_id, _g1 = await _mk_team(session, [tenant_a.id])

    # Seed a pre-existing snapshot member so we can assert it is left untouched.
    upn = f"stale-{uuid.uuid4().hex}@x.example"
    await member_repo.reconcile_snapshot(session, g1_id, [_member(upn)])

    _patch_graph(monkeypatch, {}, raises=GraphError("403: forbidden", code="graph_error"))
    with pytest.raises(group_sync.GroupSyncError) as exc:
        await group_sync.sync_group(session, {}, g1_id)
    assert exc.value.code == "sync_failed"

    # Snapshot unchanged -- the pre-existing member is still there, nothing was reconciled away.
    assert await member_repo.upns_for_group(session, g1_id) == {upn}


# ---- Missing group -> 404-style NotFoundError -------------------------------------------- #


async def test_missing_group_raises_not_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import NotFoundError

    _patch_graph(monkeypatch, {})
    with pytest.raises(NotFoundError) as exc:
        await group_sync.sync_group(session, {}, 999_999)
    assert exc.value.code == "group_not_found"
