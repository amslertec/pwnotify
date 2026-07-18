"""SECURITY ACCEPTANCE GATE for the group-member sync (Task 3, `services/group_sync.sync_group`).

This is the BROADER adversarial matrix that proves the tenant-isolation invariant holds THROUGH
the sync path -- end to end, including RLS. It is TEST-ONLY: no production code is touched. A red
cell here means the sync has a hole and blocks the branch.

Relation to `test_group_sync_adversarial.py` (Task 3): that file proves the per-call sync
behaviour cell by cell. This file re-asserts the core cells in explicit MATRIX form (default +
customers A/B, provider vs. customer/NULL-home, role-flip, revoke-vs-manual) AND adds the two
cells the Task-3 file does not cover: the RLS backstop end-to-end (cell 5) and the
no-new-grant-path structural proof (cell 6). Where a cell overlaps the Task-3 file it is called
out in a comment -- it is re-asserted here for a self-contained acceptance gate, not by accident.

Seeding + fake-Graph seam are REUSED verbatim from the Task-3 module so the two suites cannot
drift apart (no reinvented fixtures). The savepoint-isolated `session` fixture (conftest.py)
cleans up: `add_grant`/`remove_grant` commit internally, under the fixture those are savepoints,
the outer rollback leaves the suite residue-free. The RLS data-plane assertion in cell 5 needs
REAL committed rows (a tenant-scoped session opens its OWN connection and cannot see the
fixture's uncommitted savepoint data) -- it therefore seeds via a dedicated committed connection
on `migrated_engine` and cleans up in a `finally`, exactly like `test_isolation_attack.py`.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.tenant_context import tenant_scoped_session
from app.models.tenant import AdminTenant, AuditorTenant
from app.repositories import assignment_group_member_repo as member_repo
from app.repositories import assignment_group_repo, tenant_repo
from app.services import group_sync
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# REUSE the Task-3 seeding helpers + fake-Graph seam verbatim -- one source of truth so the two
# adversarial suites stay consistent (brief: "REUSE its seeding helpers and fake-Graph seam").
from tests.test_group_sync_adversarial import (
    _admin_rows,
    _auditor_rows,
    _member,
    _mk_team,
    _mk_tenant,
    _mk_user,
    _patch_graph,
)


def _upn(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}@provider.example"


async def _all_grant_rows(session: AsyncSession) -> set[tuple[str, int, int, str]]:
    """Full snapshot of BOTH grant tables as a comparable set -- (kind, user_id, tenant_id,
    source). Used by cell 6 to prove a sync over customer-homed members mutates nothing."""
    admin = (
        await session.execute(
            select(AdminTenant.user_id, AdminTenant.tenant_id, AdminTenant.source)
        )
    ).all()
    auditor = (
        await session.execute(
            select(AuditorTenant.user_id, AuditorTenant.tenant_id, AuditorTenant.source)
        )
    ).all()
    rows = {("admin", int(u), int(t), str(s)) for u, t, s in admin}
    rows |= {("auditor", int(u), int(t), str(s)) for u, t, s in auditor}
    return rows


# ========================================================================================== #
# CELL 1 -- Provider member materialized: grant lands on the team's tenant only, and on BOTH
#           tenants when the UPN sits in both teams. Asserts DIRECTLY on `admin_tenant`.
#           (Overlaps the Task-3 file's `test_provider_member_materialized_for_team_tenant_only`
#           for the single-team half; the both-teams half is matrix-only.)
# ========================================================================================== #


async def test_cell1_provider_materialized_team_tenant_only_then_both(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    g1_id, g1 = await _mk_team(session, [a.id])
    g2_id, g2 = await _mk_team(session, [b.id])

    upn = _upn("provider")
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    # Phase 1: UPN only in T1 -> EXACTLY admin_tenant(A, group), NO B row anywhere.
    _patch_graph(monkeypatch, {g1: [_member(upn)], g2: []})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {(a.id, "group")}
    assert await _auditor_rows(session, p.id) == []

    # Phase 2: UPN now in BOTH teams; sync both -> A AND B, both source='group'.
    _patch_graph(monkeypatch, {g1: [_member(upn)], g2: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)
    await group_sync.sync_group(session, {}, g2_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (a.id, "group"),
        (b.id, "group"),
    }
    assert await _auditor_rows(session, p.id) == []


# ========================================================================================== #
# CELL 2 -- THE INVARIANT: a customer-homed member AND a NULL-home member whose UPN is in T2's
#           fetched members receive ZERO group grant (admin_tenant AND auditor_tenant), no matter
#           which team's snapshot holds them. Asserts on the grant tables DIRECTLY.
#           The `is_provider_account` gate inside `reconcile_group_grants` is the only thing that
#           stops a foreign B grant from forming -- see the non-vacuity note in the report.
# ========================================================================================== #


async def test_cell2_customer_and_null_home_members_zero_grant(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    # T2 -> B. Both hostile accounts sit in T2's fetched members. Without the gate the customer-A
    # admin would gain admin_tenant(B, group) and the NULL-home account likewise -- a cross-tenant
    # leak. The gate must short-circuit both to a no-op.
    g2_id, g2 = await _mk_team(session, [b.id])

    cust_upn = _upn("customer-a")
    null_upn = _upn("null-home")
    cust = await _mk_user(session, upn=cust_upn, role="admin", tenant_id=a.id)
    nulluser = await _mk_user(session, upn=null_upn, role="admin", tenant_id=None)
    assert cust.id is not None and nulluser.id is not None

    _patch_graph(monkeypatch, {g2: [_member(cust_upn), _member(null_upn)]})
    result = await group_sync.sync_group(session, {}, g2_id)

    # ZERO group grant rows for EITHER account, in EITHER table -- assert on the tables directly.
    assert await _admin_rows(session, cust.id) == []
    assert await _auditor_rows(session, cust.id) == []
    assert await _admin_rows(session, nulluser.id) == []
    assert await _auditor_rows(session, nulluser.id) == []
    # Both are present in the SNAPSHOT (matched by username) but neither is materialized.
    assert result["materialized"] == 0
    assert result["member_count"] == 2
    snap = await member_repo.upns_for_group(session, g2_id)
    assert {cust_upn, null_upn} <= snap


# ========================================================================================== #
# CELL 3 -- Team-leave revokes the source='group' grant on the next sync; a source='manual' grant
#           persists untouched. Asserts on `admin_tenant`.
#           (Overlaps the Task-3 file's `test_team_leave_revokes_group_grant_manual_persists`.
#           The manual grant lives on a DISTINCT tenant B: a manual and a group row cannot coexist
#           on the same (user, tenant, kind) -- composite PK + manual precedence -- so a "group
#           removed on A" assertion is only non-vacuous when manual sits elsewhere.)
# ========================================================================================== #


async def test_cell3_team_leave_revokes_group_manual_persists(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    g1_id, g1 = await _mk_team(session, [a.id])

    upn = _upn("provider")
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    # Pre-existing MANUAL grant on B (explicit superadmin action), unrelated to any team.
    await tenant_repo.add_grant(
        session, user_id=p.id, tenant_id=b.id, kind="admin", source="manual"
    )

    # Sync 1: UPN in T1 -> group A materialized; manual B untouched.
    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (a.id, "group"),
        (b.id, "manual"),
    }

    # Sync 2: UPN removed from T1 -> group A revoked; manual B survives as 'manual'.
    _patch_graph(monkeypatch, {g1: []})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {(b.id, "manual")}


# ========================================================================================== #
# CELL 4 -- Role drives the grant KIND through the sync, and a role flip leaves ZERO stale group
#           row in the other table. Asserts on `auditor_tenant` then `admin_tenant`.
#           (Single-role half overlaps the Task-3 file's
#           `test_role_drives_kind_provider_auditor_gets_auditor_grant`; the flip half is
#           matrix-only and proves the reconcile's role-flip cleanup via the SYNC path.)
# ========================================================================================== #


async def test_cell4_role_drives_kind_and_flip_clears_stale(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    a = await _mk_tenant(session)
    assert a.id is not None
    # AUDITOR team: its customer A lands in auditor_tenant (the TEAM's role drives the kind).
    g1_id, g1 = await _mk_team(session, [a.id], role="auditor")

    upn = _upn("provider-auditor")
    u = await _mk_user(session, upn=upn, role="auditor", tenant_id=default.id)
    assert u.id is not None

    # Auditor team -> auditor_tenant(A, group), NEVER an admin_tenant row.
    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _auditor_rows(session, u.id)} == {
        (a.id, "group")
    }
    assert await _admin_rows(session, u.id) == []

    # Flip the TEAM's role auditor -> admin, then re-sync. The per-table reconcile must
    # materialize the grant in the NEW target table AND clear the now-stale group row in the
    # OTHER table (this is the role-flip cleanup, proven through the SYNC path).
    grp = await assignment_group_repo.get_by_entra_group_id(session, g1)
    assert grp is not None and grp.id is not None
    await assignment_group_repo.update(session, grp.id, name=grp.name, role="admin")
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, u.id)} == {(a.id, "group")}
    # ZERO stale auditor_tenant(group) row remains -- proven through the sync path.
    assert await _auditor_rows(session, u.id) == []


# ========================================================================================== #
# CELL 5 -- RLS backstop end-to-end. Two complementary planes:
#   (control plane, tied to the sync, on the fixture session) after sync grants P
#     admin_tenant(A, group): is_allowed(P, A, write) is True; a FORGED active-tenant for B is
#     denied at the gate because P has no B grant (is_allowed False). Removing P from T1 +
#     re-sync then denies A too.
#   (data plane, RLS, on REAL committed rows) a tenant-scoped session for A returns A's data and
#     ONLY A's; scoping to B never leaks A's rows -- the RLS backstop that sits behind the gate.
#     Committed seed via a dedicated connection + finally-cleanup (the fixture's savepoint data is
#     invisible to the separate connection a tenant-scoped session opens) -- same seam as
#     test_isolation_attack.py.
# ========================================================================================== #


async def test_cell5_rls_backstop_control_plane(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    g1_id, g1 = await _mk_team(session, [a.id])

    upn = _upn("provider")
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    # Sync grants P admin_tenant(A, group).
    _patch_graph(monkeypatch, {g1: [_member(upn)]})
    await group_sync.sync_group(session, {}, g1_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {(a.id, "group")}

    # Control-plane gate: A allowed (write), forged B denied -- P has no B grant.
    assert await tenant_repo.is_allowed(session, p, a.id, write=True) is True
    assert await tenant_repo.is_allowed(session, p, b.id, write=True) is False
    assert await tenant_repo.is_allowed(session, p, b.id, write=False) is False

    # Remove P from T1 + re-sync -> group A revoked -> A now denied too.
    _patch_graph(monkeypatch, {g1: []})
    await group_sync.sync_group(session, {}, g1_id)
    assert await _admin_rows(session, p.id) == []
    assert await tenant_repo.is_allowed(session, p, a.id, write=True) is False


async def test_cell5_rls_backstop_data_plane(migrated_engine: AsyncEngine) -> None:
    """RLS data-plane backstop on REAL committed rows: a tenant-scoped session sees only the
    active tenant's rows; a forged active-tenant for the other tenant never leaks the first
    tenant's data. This is the last line behind the is_allowed gate proven above."""
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                "('MtxA','mtx-a',true,now()), ('MtxB','mtx-b',true,now())"
            )
        )
        a, b = (
            (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE slug IN ('mtx-a','mtx-b') ORDER BY slug")
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            text(
                "INSERT INTO run "
                "(tenant_id, trigger, dry_run, status, started_at, "
                "checked_users, sent, failed, skipped, detail_log) VALUES "
                "(:a,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb), "
                "(:b,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb)"
            ),
            {"a": a, "b": b},
        )
        await conn.commit()
        try:
            # Scoped to A -> only A's run row. Never B.
            async with tenant_scoped_session(a) as s:
                rows_a = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
            assert set(rows_a) == {a}, f"RLS leak while scoped to A: {rows_a}"

            # Forged active-tenant for B -> only B's row; A's data never appears here.
            async with tenant_scoped_session(b) as s:
                rows_b = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
            assert set(rows_b) == {b}, f"RLS leak while scoped to B: {rows_b}"
            assert a not in set(rows_b), "Cross-tenant leak: A visible under B's scope"
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


# ========================================================================================== #
# CELL 6 -- No new grant path. A sync over a group whose ONLY members are customer-homed /
#           NULL-home must produce ZERO grant-table writes: snapshot BOTH grant tables before and
#           after, assert identical. Structural proof that the only grant mutations come from the
#           gated `reconcile_group_grants` (which no-ops for non-provider accounts).
# ========================================================================================== #


async def test_cell6_no_new_grant_path_for_customer_members(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    g_id, g = await _mk_team(session, [b.id])  # team -> B

    cust_upn = _upn("customer-a")
    null_upn = _upn("null-home")
    await _mk_user(session, upn=cust_upn, role="admin", tenant_id=a.id)
    await _mk_user(session, upn=null_upn, role="auditor", tenant_id=None)

    _patch_graph(monkeypatch, {g: [_member(cust_upn), _member(null_upn)]})

    before = await _all_grant_rows(session)
    result = await group_sync.sync_group(session, {}, g_id)
    after = await _all_grant_rows(session)

    # The grant tables are byte-for-byte identical -- no write path fired for customer members.
    assert before == after
    assert result["materialized"] == 0
    # Sanity: the members DID land in the snapshot, so the sync genuinely ran over them.
    assert {cust_upn, null_upn} <= await member_repo.upns_for_group(session, g_id)
