"""Task 2 der Multi-Tenant-Phase: Cross-Grant-Lock in `admin_assignments.set_assignments`
(`app/api/routes/admin_assignments.py`) + die Predicate `tenant_repo.is_provider_account`.

Angriffs-orientiert -- das ist der Kern dieser Task: ein Kunden-homed Konto (Heim-Tenant
ist NICHT der Default-Tenant, oder `tenant_id is None`) darf STRUKTURELL nie auf einen
fremden Tenant berechtigt werden, selbst wenn der Superadmin selbst das anfragt. Treibt
`set_assignments` direkt an (wie `test_admin_assignments.py`) -- die savepoint-isolierte
`session`-Fixture genügt für Aufräumen, kein manuelles Rollback nötig (äusserer Rollback in
`conftest.session` macht die Suite rückstandsfrei, zweimal hintereinander ausführbar).

Diese Tests sind NICHT-VAKUOS: gegen den Vor-Lock-Code (ohne die `is_provider_account`-Prüfung
in `set_assignments`) würde z. B. `test_customer_a_homed_admin_cannot_be_granted_foreign_tenant_b`
grün durchlaufen (die Zuweisung auf B würde anstandslos geschrieben) -- erst der Lock lässt ihn
mit `ForbiddenError("customer_account_not_grantable")` fehlschlagen.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.routes.admin_assignments import set_assignments
from app.core.errors import ConflictError, ForbiddenError
from app.models.tenant import AdminTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.assignment import AssignmentUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t2-cgl-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="T2 CGL Tenant", slug=_slug())
    if not active:
        assert t.id is not None
        t = await tenant_repo.update(session, t.id, is_active=False)
    return t


async def _mk_user(
    session: AsyncSession,
    *,
    role: str,
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    u = AppUser(
        username=f"t2-cgl-{role}-{uuid.uuid4().hex[:8]}",
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


# ---- is_provider_account predicate --------------------------------------------------------- #


async def test_is_provider_account_true_only_for_default_tenant_home(
    session: AsyncSession,
) -> None:
    default = await tenant_repo.default_tenant(session)
    other = await _mk_tenant(session)
    assert other.id is not None

    provider = await _mk_user(session, role="admin", tenant_id=default.id)
    customer = await _mk_user(session, role="admin", tenant_id=other.id)
    homeless = await _mk_user(session, role="admin", tenant_id=None)

    assert await tenant_repo.is_provider_account(session, provider) is True
    assert await tenant_repo.is_provider_account(session, customer) is False
    assert await tenant_repo.is_provider_account(session, homeless) is False


# ---- Attack: customer-A-homed local admin --------------------------------------------------- #


async def test_customer_a_homed_admin_cannot_be_granted_foreign_tenant_b(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    customer_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            customer_admin.id,
            AssignmentUpdate(tenant_ids=[tenant_b.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"
    assert await _admin_row(session, customer_admin.id, tenant_b.id) is None


async def test_customer_a_homed_admin_can_be_granted_its_own_home(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    customer_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    out = await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        customer_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )
    assert out.tenant_ids == [tenant_a.id]
    assert await _admin_row(session, customer_admin.id, tenant_a.id) is not None


async def test_customer_a_homed_admin_mixed_request_is_rejected_wholesale(
    session: AsyncSession,
) -> None:
    """Own home (A) mixed with a foreign tenant (B) in the SAME request must reject the
    WHOLE request -- no partial write of the A grant either."""
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    customer_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_admin.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            customer_admin.id,
            AssignmentUpdate(tenant_ids=[tenant_a.id, tenant_b.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"
    assert await _admin_row(session, customer_admin.id, tenant_a.id) is None
    assert await _admin_row(session, customer_admin.id, tenant_b.id) is None


# ---- Attack: customer-A-homed SSO auditor --------------------------------------------------- #


async def test_customer_a_homed_sso_auditor_cannot_be_granted_foreign_tenant_b(
    session: AsyncSession,
) -> None:
    """Same lock for an SSO account and a read-only role -- proves the lock keys on HOME,
    not on role or on is_sso."""
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    customer_auditor = await _mk_user(session, role="auditor", is_sso=True, tenant_id=tenant_a.id)
    assert customer_auditor.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            customer_auditor.id,
            AssignmentUpdate(tenant_ids=[tenant_b.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"


# ---- Provider account is unaffected --------------------------------------------------------- #


async def test_provider_homed_admin_can_still_be_cross_granted_both_tenants(
    session: AsyncSession,
) -> None:
    """Regression: a provider-homed (default-tenant) local admin is the whole point of this
    route -- the superadmin must still be able to cross-grant it any active tenant."""
    superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    provider_admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider_admin.id is not None

    out = await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        provider_admin.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id, tenant_b.id]),
        session,
    )
    assert set(out.tenant_ids) == {tenant_a.id, tenant_b.id}
    assert await _admin_row(session, provider_admin.id, tenant_a.id) is not None
    assert await _admin_row(session, provider_admin.id, tenant_b.id) is not None


# ---- Home-NULL edge account: default-deny --------------------------------------------------- #


async def test_home_null_account_is_denied_any_non_empty_grant(session: AsyncSession) -> None:
    """A `tenant_id is None` account is NOT a provider account (default-deny in
    `is_provider_account`) and has no home to fall back to -- `allowed` is the empty set,
    so ANY non-empty request is rejected."""
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    homeless_admin = await _mk_user(session, role="admin", tenant_id=None)
    assert homeless_admin.id is not None

    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            homeless_admin.id,
            AssignmentUpdate(tenant_ids=[tenant_a.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"
    assert await _admin_row(session, homeless_admin.id, tenant_a.id) is None


# ---- Regressions: existing guards still fire, in order -------------------------------------- #


async def test_superadmin_target_still_rejected_before_cross_grant_lock(
    session: AsyncSession,
) -> None:
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


async def test_inactive_tenant_still_rejected_for_provider_account(
    session: AsyncSession,
) -> None:
    """The active-tenant check still runs AFTER the cross-grant lock for a provider
    account (which passes the lock unconditionally)."""
    superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    inactive = await _mk_tenant(session, active=False)
    assert inactive.id is not None
    provider_admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider_admin.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            provider_admin.id,
            AssignmentUpdate(tenant_ids=[inactive.id]),
            session,
        )
    assert exc_info.value.code == "tenant_not_active"
    assert await _admin_row(session, provider_admin.id, inactive.id) is None
