"""Task 2 der Console+Groups+Invite-Phase: `PUT /admin/assignments/bulk`
(`app/api/routes/admin_assignments.py`, `bulk_assign`) -- die Bulk-Variante MUSS denselben
Cross-Grant-Lock durchsetzen wie die Einzel-Zuweisung (`set_assignments`,
`test_assignment_cross_grant_lock.py`), pro Konto der Charge, ohne eine Ausnahme.

Angriffs-orientiert -- das ist der Kern dieser Task: ein Kunden-homed Konto (Heim-Tenant
ist NICHT der Default-Tenant) darf STRUKTURELL nie auf einen fremden Tenant berechtigt
werden, selbst innerhalb einer Batch-Anfrage, in der andere (Provider-)Konten legitim
cross-granted werden. `bulk_assign` und `set_assignments` rufen dafür denselben privaten
Helfer `_cross_grant_lock_allows` -- diese Tests beweisen das Verhalten der Route, nicht
den Helfer isoliert.

NICHT-VAKUOS: gegen eine (hypothetische) Bulk-Route ohne den Lock würde
`test_bulk_set_customer_a_homed_admin_is_skipped_provider_accounts_still_updated` z. B. CA
klaglos auf B schreiben -- erst `_cross_grant_lock_allows` lässt CA mit
`customer_account_not_grantable` überspringen, während P1/P2 in DERSELBEN Anfrage trotzdem
aktualisiert werden.

Treibt `bulk_assign` direkt an (wie `test_admin_assignments.py`/
`test_assignment_cross_grant_lock.py`) -- die savepoint-isolierte `session`-Fixture
(`conftest.py`) genügt für Aufräumen: der äussere Rollback macht die Suite rückstandsfrei,
zweimal hintereinander ausführbar, kein manuelles `finally`-Aufräumen nötig, exakt wie bei
den beiden oben genannten Testdateien für dieselbe Route-Familie.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import ACCESS_COOKIE, require_superadmin_default_context
from app.api.routes.admin_assignments import bulk_assign
from app.core.errors import ConflictError, ForbiddenError
from app.core.security import issue_token_pair
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.assignment import BulkAssignmentUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRequest:
    """Duck-typed Request -- wie `test_matrix_b_route_gating.py`s `_FakeRequest`: Guard und
    Route lesen nur `.cookies`/`.headers`/`.client` (Audit-Aufrufe in den Erfolgsfällen)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _slug() -> str:
    return f"t2-bulk-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="T2 Bulk Tenant", slug=_slug())
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
        username=f"t2-bulk-{role}-{uuid.uuid4().hex[:8]}",
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


async def _auditor_row(session: AsyncSession, user_id: int, tenant_id: int) -> AuditorTenant | None:
    return (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == user_id, AuditorTenant.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


async def _all_admin_rows(session: AsyncSession, user_id: int) -> list[AdminTenant]:
    return list(
        (await session.execute(select(AdminTenant).where(AdminTenant.user_id == user_id))).scalars()
    )


# ---- Mixed batch: customer-homed account skipped, provider-homed accounts still applied --- #


async def test_bulk_set_customer_a_homed_admin_is_skipped_provider_accounts_still_updated(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None

    p1 = await _mk_user(session, role="admin", tenant_id=default.id)
    p2 = await _mk_user(session, role="admin", tenant_id=default.id)
    customer_a_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert p1.id is not None and p2.id is not None and customer_a_admin.id is not None

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        superadmin,
        BulkAssignmentUpdate(
            user_ids=[p1.id, p2.id, customer_a_admin.id],
            tenant_ids=[tenant_a.id, tenant_b.id],
            action="set",
        ),
        session,
    )

    assert sorted(out.updated) == sorted([p1.id, p2.id])
    assert len(out.skipped) == 1
    assert out.skipped[0].user_id == customer_a_admin.id
    assert out.skipped[0].reason == "customer_account_not_grantable"

    # CA got NOTHING written -- neither the foreign B nor even its own home A (the whole
    # request for this account was rejected, not a partial application).
    assert await _all_admin_rows(session, customer_a_admin.id) == []

    # P1/P2 hold BOTH tenants, written with source='manual' (an explicit bulk admin action).
    for provider in (p1, p2):
        assert provider.id is not None
        row_a = await _admin_row(session, provider.id, tenant_a.id)
        row_b = await _admin_row(session, provider.id, tenant_b.id)
        assert row_a is not None and row_a.source == "manual"
        assert row_b is not None and row_b.source == "manual"


async def test_bulk_add_customer_b_homed_auditor_cannot_be_granted_foreign_tenant_a(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    customer_b_auditor = await _mk_user(session, role="auditor", tenant_id=tenant_b.id)
    assert customer_b_auditor.id is not None

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        superadmin,
        BulkAssignmentUpdate(
            user_ids=[customer_b_auditor.id], tenant_ids=[tenant_a.id], action="add"
        ),
        session,
    )

    assert out.updated == []
    assert len(out.skipped) == 1
    assert out.skipped[0].user_id == customer_b_auditor.id
    assert out.skipped[0].reason == "customer_account_not_grantable"
    assert await _auditor_row(session, customer_b_auditor.id, tenant_a.id) is None


async def test_bulk_set_customer_a_homed_admin_can_be_granted_its_own_home(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    customer_a_admin = await _mk_user(session, role="admin", tenant_id=tenant_a.id)
    assert customer_a_admin.id is not None

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        superadmin,
        BulkAssignmentUpdate(
            user_ids=[customer_a_admin.id], tenant_ids=[tenant_a.id], action="set"
        ),
        session,
    )

    assert out.updated == [customer_a_admin.id]
    assert out.skipped == []
    row = await _admin_row(session, customer_a_admin.id, tenant_a.id)
    assert row is not None and row.source == "manual"


# ---- Superadmin target + unknown user id, both skipped, rest of batch still applied ------- #


async def test_bulk_superadmin_target_and_unknown_user_are_skipped(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    other_superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert other_superadmin.id is not None and tenant_a.id is not None
    provider_admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider_admin.id is not None
    unknown_user_id = 9_999_999

    out = await bulk_assign(
        None,  # type: ignore[arg-type]
        superadmin,
        BulkAssignmentUpdate(
            user_ids=[other_superadmin.id, unknown_user_id, provider_admin.id],
            tenant_ids=[tenant_a.id],
            action="add",
        ),
        session,
    )

    assert out.updated == [provider_admin.id]
    reasons = {s.user_id: s.reason for s in out.skipped}
    assert reasons[other_superadmin.id] == "cannot_assign_superadmin"
    assert reasons[unknown_user_id] == "user_not_found"
    assert await _admin_row(session, provider_admin.id, tenant_a.id) is not None


# ---- Bad tenant id anywhere in the batch: hard-fail the WHOLE request, zero writes -------- #


async def test_bulk_inactive_tenant_id_hard_fails_whole_request_with_zero_writes(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    inactive = await _mk_tenant(session, active=False)
    assert tenant_a.id is not None and inactive.id is not None
    p1 = await _mk_user(session, role="admin", tenant_id=default.id)
    p2 = await _mk_user(session, role="admin", tenant_id=default.id)
    assert p1.id is not None and p2.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await bulk_assign(
            None,  # type: ignore[arg-type]
            superadmin,
            BulkAssignmentUpdate(
                user_ids=[p1.id, p2.id],
                tenant_ids=[tenant_a.id, inactive.id],
                action="set",
            ),
            session,
        )
    assert exc_info.value.code == "tenant_not_active"

    # NOTHING partial -- the bad tenant id is validated BEFORE any user in the batch is
    # touched, so neither P1 nor P2 got even the valid tenant_a grant.
    assert await _all_admin_rows(session, p1.id) == []
    assert await _all_admin_rows(session, p2.id) == []


async def test_bulk_unknown_tenant_id_hard_fails_whole_request(session: AsyncSession) -> None:
    superadmin = await _mk_user(session, role="superadmin")
    default = await tenant_repo.default_tenant(session)
    provider_admin = await _mk_user(session, role="admin", tenant_id=default.id)
    assert provider_admin.id is not None

    with pytest.raises(ConflictError) as exc_info:
        await bulk_assign(
            None,  # type: ignore[arg-type]
            superadmin,
            BulkAssignmentUpdate(
                user_ids=[provider_admin.id], tenant_ids=[9_999_999], action="add"
            ),
            session,
        )
    assert exc_info.value.code == "tenant_not_active"
    assert await _all_admin_rows(session, provider_admin.id) == []


# ---- Same superadmin, switched to a customer context -> 403 default_context_required ------ #


async def test_bulk_blocked_from_customer_context(session: AsyncSession) -> None:
    """Whole-Branch invariant (Matrix B, `test_matrix_b_route_gating.py`): the assignment
    console -- bulk included -- is Provider-Ebene and reachable only from the Superadmin's
    DEFAULT context. Switching the SAME superadmin's active tenant to a customer must block
    `bulk_assign` with `default_context_required`, exactly like the single-user route."""
    superadmin = await _mk_user(session, role="superadmin")
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="T2 Bulk Customer Ctx", slug=_slug())
    assert customer.id is not None
    provider_admin = await _mk_user(session, role="admin")
    tenant_a = await _mk_tenant(session)
    assert provider_admin.id is not None and tenant_a.id is not None

    pair = issue_token_pair(str(superadmin.id), active_tenant=customer.id)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await bulk_assign(
            request,  # type: ignore[arg-type]
            guarded,
            BulkAssignmentUpdate(
                user_ids=[provider_admin.id], tenant_ids=[tenant_a.id], action="add"
            ),
            session,
        )
    assert exc_info.value.code == "default_context_required"
    assert await _all_admin_rows(session, provider_admin.id) == []
