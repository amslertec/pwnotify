"""TDD für Task 3 des Context-Gating-v2-Increments: `create_local` setzt jetzt den
HEIM-Tenant (`AppUser.tenant_id`) des neuen Kontos, nicht nur die Zuweisungszeile.

**THE bug, den dieser Test beweist:** vormals bekam ein neu angelegtes lokales Konto zwar
eine `admin_tenant`/`auditor_tenant`-Zuweisung, aber NIE eine Heimat (`tenant_id` blieb
`None`). Der Cross-Grant-Lock aus Task 2 (`tenant_repo.is_provider_account`,
`admin_assignments.set_assignments`) prüft aber genau diese Heimat -- ohne sie griff der
Lock nicht wie gedacht. Dieser Test beweist END-TO-END (nicht nur den rohen `tenant_id`-Wert),
dass der Lock nach dem Fix tatsächlich bissig ist:

- Ein von einem Kunden-Admin angelegtes Konto ist kunden-beheimatet -> die Zuweisungs-API
  lehnt eine Fremdzuweisung mit `customer_account_not_grantable` ab.
- Ein von einem Superadmin angelegtes Konto ist default-beheimatet (Provider) -> die
  Zuweisungs-API lässt eine Fremdzuweisung zu.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.routes.admin_assignments import set_assignments
from app.api.routes.admin_users import create_local
from app.core.errors import ForbiddenError
from app.models.tenant import AdminTenant, AuditorTenant, Tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.assignment import AssignmentUpdate
from app.schemas.auth import AdminUserCreate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _slug() -> str:
    return f"t3home-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession) -> Tenant:
    return await tenant_repo.create(session, name="T3-Home Tenant", slug=_slug())


async def _mk_user(
    session: AsyncSession, *, role: str, is_sso: bool = False, tenant_id: int | None = None
) -> AppUser:
    u = AppUser(
        username=f"t3home-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def test_customer_admin_creates_admin_home_is_active_tenant_and_lock_bites(
    session: AsyncSession,
) -> None:
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    superadmin = await _mk_user(session, role="superadmin")
    local_admin_a = await _mk_user(session, role="admin")
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))
    await session.flush()

    body = AdminUserCreate(
        username=f"t3home-new-admin-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="admin",
    )
    out = await create_local(
        None,  # type: ignore[arg-type]
        local_admin_a,
        body,
        session,
        tenant_a.id,
    )

    # Heimat gesetzt: das neue Konto gehört jetzt Tenant A, nicht mehr "None".
    # `AdminUserOut` trägt kein `tenant_id`-Feld -- direkt am persistierten `AppUser` prüfen.
    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == tenant_a.id
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_a.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "Neues Konto hat keine admin_tenant(A)-Zuweisung erhalten"

    # Non-vakuöser Beweis: der Task-2-Lock greift jetzt WIRKLICH, weil eine Heimat existiert
    # -- ein Versuch, das neue (kunden-beheimatete) Konto zusätzlich auf B zu berechtigen,
    # scheitert an `customer_account_not_grantable`.
    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            out.id,
            AssignmentUpdate(tenant_ids=[tenant_b.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"
    # Kein Schreibversuch hat durchgeschlagen -- weiterhin nur die A-Zuweisung.
    b_row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_b.id
            )
        )
    ).scalar_one_or_none()
    assert b_row is None


async def test_customer_admin_creates_auditor_home_is_active_tenant_with_auditor_grant(
    session: AsyncSession,
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    local_admin_a = await _mk_user(session, role="admin")
    assert local_admin_a.id is not None
    session.add(AdminTenant(user_id=local_admin_a.id, tenant_id=tenant_a.id))
    await session.flush()

    body = AdminUserCreate(
        username=f"t3home-new-auditor-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="auditor",
    )
    out = await create_local(
        None,  # type: ignore[arg-type]
        local_admin_a,
        body,
        session,
        tenant_a.id,
    )

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == tenant_a.id
    auditor_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == tenant_a.id
            )
        )
    ).scalar_one_or_none()
    assert auditor_row is not None, "Neuer Auditor hat keine auditor_tenant(A)-Zuweisung erhalten"

    # NIE eine admin_tenant-Zeile für ein Auditor-Ziel (Grant-Typ muss zur Rolle passen).
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None


async def test_superadmin_creates_admin_home_is_default_and_cross_grantable(
    session: AsyncSession,
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    superadmin = await _mk_user(session, role="superadmin")

    body = AdminUserCreate(
        username=f"t3home-super-created-{uuid.uuid4().hex[:8]}",
        password="a-strong-password-1",
        role="admin",
    )
    out = await create_local(None, superadmin, body, session, None)  # type: ignore[arg-type]

    # Provider-beheimatet: die Heimat ist der Default-Tenant, KEINE automatische Zuweisung.
    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == default.id
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None

    # Non-vakuöser Beweis: weil die Heimat der Default-Tenant ist (`is_provider_account`),
    # lässt die Zuweisungs-API den Superadmin das neue Konto trotzdem auf einen Kunden (A)
    # cross-grant -- der Lock sperrt nur kunden-beheimatete Konten, nicht dieses hier.
    assert out.id is not None
    result = await set_assignments(
        None,  # type: ignore[arg-type]
        superadmin,
        out.id,
        AssignmentUpdate(tenant_ids=[tenant_a.id]),
        session,
    )
    assert result.tenant_ids == [tenant_a.id]
    row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_a.id
            )
        )
    ).scalar_one_or_none()
    assert row is not None
