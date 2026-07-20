"""TDD for the per-tenant lockout protection (A4).

**THE bug this test proves:** the last-admin guard in `set_role` counts via
`user_repo.count_admins` INSTANCE-WIDE -- it only prevents the instance from having NO
admin left at all, not a SINGLE customer losing its last (write) admin. `delete_user`
had no admin-count guard at all. Attack chain: customer A has two admins a1/a2, an admin b1
in customer B holds the instance-wide count > 1. a1 demotes/deletes a2, then themselves ->
A has zero write admins, only the provider superadmin can still rescue it.

The fix counts PER TENANT (`user_repo.count_tenant_admins`) and blocks demoting/
deleting the last admin of a customer with `code="last_tenant_admin"` -- even if other
admins still exist instance-wide.

Drives the route functions directly (like `test_admin_users_scoping.py`); the savepoint-
isolated `session` fixture (real Postgres) keeps the suite residue-free. The caller is
consistently a superadmin -- it skips the scope check, so the per-tenant guard under
test here is reached cleanly (the cross-tenant scope check itself is covered by
`test_admin_users_scoping.py`)."""

from __future__ import annotations

import uuid

import pytest
from app.api.routes.admin_assignments import bulk_assign, set_assignments
from app.api.routes.admin_users import delete_user, set_role
from app.core.errors import ConflictError
from app.models.audit import AuditLog
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo, user_repo
from app.schemas.assignment import AssignmentUpdate, BulkAssignmentUpdate
from app.schemas.auth import RoleUpdate
from app.services import audit
from sqlalchemy import select
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
    """Local (or SSO) account, optionally with an `admin_tenant` assignment. `grant_tenant_id`
    controls the grant row; without it (SSO home admin) the account carries only its `tenant_id`."""
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


# ---- count_tenant_admins: the counting semantics ------------------------------------------ #


async def test_count_tenant_admins_counts_grants_and_sso_home_not_superadmin_not_inactive(
    session: AsyncSession,
) -> None:
    """`count_tenant_admins(A)` counts: local admins with an `admin_tenant(A)` grant PLUS SSO
    admins with home tenant A -- but NEVER superadmins (instance-wide, separately protected) and
    NEVER inactive/pending accounts (they can't manage anyone). Non-vacuous proof: every excluded
    category is actually populated."""
    a = await _mk_tenant(session)
    assert a.id is not None

    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)  # local admin with grant
    await _mk_admin(session, tenant_id=a.id, is_sso=True)  # SSO admin homed at A (no grant)
    # Excluded:
    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id, is_active=False)  # inactive
    sa = await _superadmin(session)  # superadmin -- never counted
    session.add(AdminTenant(user_id=sa.id, tenant_id=a.id))  # not counted even with a grant
    await session.flush()

    assert await user_repo.count_tenant_admins(session, a.id) == 2


# ---- set_role: per-tenant last-admin guard ------------------------------------------------ #


async def test_demote_last_tenant_admin_blocked_even_though_instance_count_gt_1(
    session: AsyncSession,
) -> None:
    """A has ONLY a1, B has b1 (instance-wide admin count = 2 > 1, the old guard doesn't trigger).
    Demoting a1 must still fail with `last_tenant_admin`."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)  # b1 keeps instance count > 1
    caller = await _superadmin(session)

    with pytest.raises(ConflictError) as exc_info:
        await set_role(None, caller, a1.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"

    refreshed = await session.get(AppUser, a1.id)
    assert refreshed is not None and refreshed.role == "admin"


async def test_demote_one_of_two_then_last_is_blocked(session: AsyncSession) -> None:
    """Positive control + full attack chain: as long as A has TWO admins, demoting
    ONE of them is allowed; demoting the then-remaining last one is blocked."""
    a = await _mk_tenant(session)
    b = await _mk_tenant(session)
    assert a.id is not None and b.id is not None
    a1 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    a2 = await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)

    # Positive: A has two admins -> a2 may be demoted.
    out = await set_role(None, caller, a2.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert out.role == "auditor"

    # Now a1 is the last admin of A -> demotion blocked.
    with pytest.raises(ConflictError) as exc_info:
        await set_role(None, caller, a1.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "last_tenant_admin"


async def test_demote_auditor_target_never_triggers_tenant_admin_guard(
    session: AsyncSession,
) -> None:
    """Regression guard: an auditor target has no `admin_tenant` grants -- promoting/
    changing an auditor must never get stuck on the per-tenant admin guard."""
    a = await _mk_tenant(session)
    assert a.id is not None
    await _mk_admin(session, tenant_id=a.id, grant_tenant_id=a.id)  # A keeps one admin
    auditor = await _mk_admin(session, tenant_id=a.id, role="auditor")
    caller = await _superadmin(session)

    out = await set_role(None, caller, auditor.id, RoleUpdate(role="admin"), session)  # type: ignore[arg-type]
    assert out.role == "admin"


# ---- delete_user: per-tenant last-admin guard ---------------------------------------------- #


async def test_delete_last_tenant_admin_blocked_even_though_instance_count_gt_1(
    session: AsyncSession,
) -> None:
    """`delete_user` had no admin-count guard at all: deleting the last A admin must
    now fail with `last_tenant_admin`, even though b1 still exists instance-wide."""
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


# ---- L-02: count_tenant_admins ignores a stale admin_tenant grant on an auditor ----------- #


async def test_count_tenant_admins_excludes_auditor_with_stale_admin_grant(
    session: AsyncSession,
) -> None:
    """L-02: `set_role` skips the grant migration for SSO targets (`if not target.is_sso`), so
    an SSO account demoted to `auditor` KEEPS its `admin_tenant` grant rows -- a phantom admin.
    The grant branch of `count_tenant_admins` must additionally require `role=='admin'`, so such
    a stale grant no longer inflates the per-tenant count (and no longer masks the real last
    admin from the lockout guards). Non-vacuous: a real admin with a grant on B still counts."""
    b = await _mk_tenant(session)
    assert b.id is not None
    # SSO account demoted to auditor but still holding an admin_tenant grant on B (phantom).
    await _mk_admin(session, tenant_id=b.id, role="auditor", is_sso=True, grant_tenant_id=b.id)
    # A genuine admin with a grant on B -- the only account that must count.
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)

    assert await user_repo.count_tenant_admins(session, b.id) == 1


# ---- L-01: set_assignments / bulk_assign honour the last-tenant-admin lockout ------------- #


async def _mk_auditor_with_grant(session: AsyncSession, *, tenant_id: int) -> AppUser:
    u = AppUser(
        username=f"a4-auditor-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="auditor",
        is_sso=False,
        is_active=True,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    assert u.id is not None
    session.add(AuditorTenant(user_id=u.id, tenant_id=tenant_id))
    await session.flush()
    return u


async def test_set_assignments_revoking_last_tenant_admin_is_blocked(
    session: AsyncSession,
) -> None:
    """L-01: `set_assignments(tenant_ids=[])` strips every admin grant of the target. If the
    target is the LAST admin of a customer, that revoke leaves the customer with no write admin
    -- rescuable only by the provider superadmin. Must fail with `last_tenant_admin` (consistent
    with the set_role/delete_user guards), grant left intact."""
    b = await _mk_tenant(session)
    assert b.id is not None
    admin_b = await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)
    assert admin_b.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            caller,
            admin_b.id,
            AssignmentUpdate(tenant_ids=[]),
            session,
        )
    assert exc_info.value.code == "last_tenant_admin"
    # Grant untouched.
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == admin_b.id, AdminTenant.tenant_id == b.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None


async def test_set_assignments_revoke_allowed_when_second_admin_remains(
    session: AsyncSession,
) -> None:
    """Positive control: with TWO admins on B, revoking one's grant is allowed."""
    b = await _mk_tenant(session)
    assert b.id is not None
    admin_b1 = await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)  # second admin keeps B safe
    caller = await _superadmin(session)
    assert admin_b1.id is not None

    out = await set_assignments(
        None,  # type: ignore[arg-type]
        caller,
        admin_b1.id,
        AssignmentUpdate(tenant_ids=[]),
        session,
    )
    assert out.tenant_ids == []


async def test_set_assignments_revoking_auditor_grant_never_triggers_guard(
    session: AsyncSession,
) -> None:
    """An auditor_tenant revoke can never cause a write-admin lockout -- the guard only applies
    to admin grants. Revoking the sole auditor's grant is always allowed."""
    b = await _mk_tenant(session)
    assert b.id is not None
    auditor_b = await _mk_auditor_with_grant(session, tenant_id=b.id)
    caller = await _superadmin(session)
    assert auditor_b.id is not None

    out = await set_assignments(
        None,  # type: ignore[arg-type]
        caller,
        auditor_b.id,
        AssignmentUpdate(tenant_ids=[]),
        session,
    )
    assert out.tenant_ids == []
    assert (
        await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == auditor_b.id))
    ).scalar_one_or_none() is None


async def test_bulk_assign_remove_last_tenant_admin_is_blocked(session: AsyncSession) -> None:
    """L-01 (bulk): `bulk_assign(action='remove')` of the last admin grant of a customer must
    hard-fail with `last_tenant_admin` too -- same guard, same code path expectation."""
    b = await _mk_tenant(session)
    assert b.id is not None
    admin_b = await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)
    assert admin_b.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await bulk_assign(
            None,  # type: ignore[arg-type]
            caller,
            BulkAssignmentUpdate(user_ids=[admin_b.id], tenant_ids=[b.id], action="remove"),
            session,
        )
    assert exc_info.value.code == "last_tenant_admin"
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == admin_b.id, AdminTenant.tenant_id == b.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None


async def test_bulk_assign_remove_allowed_when_second_admin_remains(
    session: AsyncSession,
) -> None:
    """Positive control for the bulk guard: two admins on B -> removing one grant is allowed."""
    b = await _mk_tenant(session)
    assert b.id is not None
    admin_b1 = await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)
    assert admin_b1.id is not None

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        caller,
        BulkAssignmentUpdate(user_ids=[admin_b1.id], tenant_ids=[b.id], action="remove"),
        session,
    )
    assert out.updated == [admin_b1.id]
    assert out.skipped == []


# ---- L-05: TENANT_ASSIGNED / TENANT_UNASSIGNED are attributed to the affected tenant ------ #


async def _audit_rows(session: AsyncSession, action: str) -> list[AuditLog]:
    return list(
        (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
    )


async def test_set_assignments_audit_is_attributed_to_affected_tenant(
    session: AsyncSession,
) -> None:
    """L-05: the TENANT_ASSIGNED / TENANT_UNASSIGNED audit entries must carry the affected
    `tenant_id`, not NULL -- otherwise the customer never sees in its own (tenant-scoped) log
    that an account gained/lost access to its tenant. Runs on the owner session, where the
    ContextVar default_factory has nothing to stamp; only an explicit `tenant_id=tid` attributes
    it."""
    default = await tenant_repo.default_tenant(session)
    provider = await _mk_admin(session, tenant_id=default.id)  # provider account, no grant yet
    b = await _mk_tenant(session)
    # A resident admin of B so revoking the provider's grant below is not a last-admin lockout.
    await _mk_admin(session, tenant_id=b.id, grant_tenant_id=b.id)
    caller = await _superadmin(session)
    assert provider.id is not None and b.id is not None

    # Assign B -> one TENANT_ASSIGNED attributed to B.
    await set_assignments(
        None,  # type: ignore[arg-type]
        caller,
        provider.id,
        AssignmentUpdate(tenant_ids=[b.id]),
        session,
    )
    assigned = await _audit_rows(session, audit.TENANT_ASSIGNED)
    assert len(assigned) == 1
    assert assigned[0].tenant_id == b.id

    # Revoke B again -> one TENANT_UNASSIGNED attributed to B.
    await set_assignments(
        None,  # type: ignore[arg-type]
        caller,
        provider.id,
        AssignmentUpdate(tenant_ids=[]),
        session,
    )
    unassigned = await _audit_rows(session, audit.TENANT_UNASSIGNED)
    assert len(unassigned) == 1
    assert unassigned[0].tenant_id == b.id


async def test_bulk_assign_audit_is_attributed_to_affected_tenant(
    session: AsyncSession,
) -> None:
    """L-05 (bulk): same attribution requirement for the bulk route's audit entries."""
    default = await tenant_repo.default_tenant(session)
    provider = await _mk_admin(session, tenant_id=default.id)
    b = await _mk_tenant(session)
    caller = await _superadmin(session)
    assert provider.id is not None and b.id is not None

    await bulk_assign(
        None,  # type: ignore[arg-type]
        caller,
        BulkAssignmentUpdate(user_ids=[provider.id], tenant_ids=[b.id], action="add"),
        session,
    )
    assigned = await _audit_rows(session, audit.TENANT_ASSIGNED)
    assert len(assigned) == 1
    assert assigned[0].tenant_id == b.id


async def test_delete_one_of_two_then_last_is_blocked(session: AsyncSession) -> None:
    """Positive control + attack chain for deletion: one of two A admins may be deleted,
    the then-remaining last one may not."""
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
