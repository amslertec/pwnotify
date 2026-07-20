"""TDD for the prod bug: `create_local` (`api/routes/admin_users.py`) ignored the active
tenant for a SUPERADMIN caller. A superadmin who switched INTO a customer and created/invited
a local account there produced it at the DEFAULT tenant WITHOUT a customer grant -- so the new
account never showed up for that customer.

Product-owner decision ("Kunden-Konto"): a superadmin in a REAL customer context
(`active_tenant` set, != default tenant, tenant exists + is active) creates a customer-homed +
customer-granted account, exactly like a customer admin would. The DEFAULT/provider context is
unchanged (provider staff, default-homed, no auto-grant).

Drives the route function directly (savepoint-isolated `session` fixture, real Postgres); the
invite path's mail send is faked via `services.user_token.build_sender`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import limiter
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


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeSender:
    backend = "fake"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
        inline_images: list[Any] | None = None,
    ) -> None:
        self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body})


@pytest_asyncio.fixture
async def fake_sender(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_FakeSender]:
    import app.services.user_token as user_token_service

    sender = _FakeSender()
    monkeypatch.setattr(user_token_service, "build_sender", lambda _settings: sender)
    yield sender


def _slug() -> str:
    return f"sactx-{uuid.uuid4().hex[:10]}"


async def _mk_tenant(session: AsyncSession, *, is_active: bool = True) -> Tenant:
    t = await tenant_repo.create(session, name="Superadmin-Ctx Tenant", slug=_slug())
    if not is_active:
        t.is_active = False
        await session.flush()
    return t


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    u = AppUser(
        username=f"sactx-super-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="superadmin",
        is_sso=False,
    )
    session.add(u)
    await session.flush()
    return u


# ---- Invite mode ------------------------------------------------------------------------- #


async def test_superadmin_in_customer_context_invite_homes_and_grants_to_customer(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """THE bug: superadmin switched into customer B invites a local admin -> the account must
    be HOMED at B and hold an `admin_tenant(B)` grant (customer-homed, like a customer admin).
    Against the old code this was RED: home=default, no grant."""
    default = await tenant_repo.default_tenant(session)
    tenant_b = await _mk_tenant(session)
    tenant_c = await _mk_tenant(session)
    assert tenant_b.id is not None and tenant_c.id is not None
    superadmin = await _mk_superadmin(session)
    email = f"invited-{uuid.uuid4().hex[:8]}@sactx.test"

    body = AdminUserCreate(email=email, role="admin")  # invite mode: no password
    out = await create_local(
        None,  # type: ignore[arg-type]
        superadmin,
        body,
        session,
        tenant_b.id,  # active tenant = the customer the superadmin switched into
    )

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.tenant_id == tenant_b.id, "invite must home at the active customer"
    assert persisted.tenant_id != default.id
    assert persisted.is_active is False
    assert persisted.username.startswith("pending:")
    grant = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_b.id
            )
        )
    ).scalar_one_or_none()
    assert grant is not None, "superadmin invite in customer context got no admin_tenant(B) grant"

    # Non-vacuous proof: because the account is customer-homed at B, the cross-grant lock bites
    # -- the superadmin cannot additionally grant it to a foreign customer C.
    assert out.id is not None
    with pytest.raises(ForbiddenError) as exc_info:
        await set_assignments(
            None,  # type: ignore[arg-type]
            superadmin,
            out.id,
            AssignmentUpdate(tenant_ids=[tenant_c.id]),
            session,
        )
    assert exc_info.value.code == "customer_account_not_grantable"


async def test_superadmin_in_customer_context_auditor_gets_auditor_grant(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """Grant kind matches the new role: an auditor invite in a customer context gets an
    `auditor_tenant` grant, never an `admin_tenant` one."""
    tenant_b = await _mk_tenant(session)
    assert tenant_b.id is not None
    superadmin = await _mk_superadmin(session)

    body = AdminUserCreate(email=f"aud-{uuid.uuid4().hex[:8]}@sactx.test", role="auditor")
    out = await create_local(None, superadmin, body, session, tenant_b.id)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == tenant_b.id
    auditor_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == out.id, AuditorTenant.tenant_id == tenant_b.id
            )
        )
    ).scalar_one_or_none()
    assert auditor_row is not None
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None


async def test_superadmin_in_default_context_invite_unchanged(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """Regression: superadmin with active_tenant == DEFAULT stays provider staff -- default-homed,
    NO auto-grant (the superadmin assigns customers later)."""
    default = await tenant_repo.default_tenant(session)
    assert default.id is not None
    superadmin = await _mk_superadmin(session)

    body = AdminUserCreate(email=f"prov-{uuid.uuid4().hex[:8]}@sactx.test", role="admin")
    out = await create_local(None, superadmin, body, session, default.id)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == default.id
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None


async def test_superadmin_in_inactive_tenant_context_falls_back_to_provider(
    session: AsyncSession, fake_sender: _FakeSender
) -> None:
    """A stale/forged active-tenant claim pointing at an INACTIVE tenant must not home the
    account there -- it falls back to provider staff (default home, no grant), same guard
    `is_allowed` enforces everywhere."""
    default = await tenant_repo.default_tenant(session)
    inactive = await _mk_tenant(session, is_active=False)
    assert inactive.id is not None
    superadmin = await _mk_superadmin(session)

    body = AdminUserCreate(email=f"stale-{uuid.uuid4().hex[:8]}@sactx.test", role="admin")
    out = await create_local(None, superadmin, body, session, inactive.id)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None and persisted.tenant_id == default.id
    admin_row = (
        await session.execute(select(AdminTenant).where(AdminTenant.user_id == out.id))
    ).scalar_one_or_none()
    assert admin_row is None


# ---- Direct mode ------------------------------------------------------------------------- #


async def test_superadmin_in_customer_context_direct_mode_homes_and_grants_to_customer(
    session: AsyncSession,
) -> None:
    """The customer-context path applies to DIRECT creation (with password) too, not only invites.
    No mail is sent here (no `fake_sender` -- a stray invite branch would fail loudly)."""
    tenant_b = await _mk_tenant(session)
    assert tenant_b.id is not None
    superadmin = await _mk_superadmin(session)
    username = f"sactx-direct-{uuid.uuid4().hex[:8]}"

    body = AdminUserCreate(username=username, password="Str0ng!Passw0rd1", role="admin")
    out = await create_local(None, superadmin, body, session, tenant_b.id)  # type: ignore[arg-type]

    persisted = await session.get(AppUser, out.id)
    assert persisted is not None
    assert persisted.username == username
    assert persisted.is_active is True
    assert persisted.tenant_id == tenant_b.id
    grant = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == out.id, AdminTenant.tenant_id == tenant_b.id
            )
        )
    ).scalar_one_or_none()
    assert grant is not None
