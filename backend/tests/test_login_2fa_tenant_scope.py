"""TDD for M2: the login handler read `auth.require_2fa` from the WRONG tenant.

`auth.require_2fa` is a regular PER-TENANT setting (no `instance.` prefix in
`settings_schema.py`): every customer governs its own 2FA requirement via `PUT /settings`.
The login handler, however, calls `SettingsService(session).get("auth.require_2fa")` on the
owner session, which carries NO tenant context -- so `get_all` falls back to the DEFAULT
tenant and reads the wrong customer's value.

Consequences (both proven below):

1. A customer B admin turns 2FA on for tenant B; a local account HOMED in tenant B logs in
   -> the check reads tenant 1 (default = off) -> the requirement does NOT bite. Security
   control governed by the wrong tenant.
2. Symmetrically, the default tenant's value is force-applied to every other tenant: default
   = on, tenant B = off -> a B account is wrongly funnelled into 2FA setup.

The fix binds the account's HOME tenant (`user.tenant_id`) for exactly this one read.

Direct route-call pattern (no HTTP) borrowed from `tests/test_login_enumeration.py`: the
owner `session` fixture (RLS bypassed by ownership) both seeds the per-tenant rows and is
passed to `login`, so uncommitted savepoint data is visible to the read under test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.routes.auth import login
from app.core.security import hash_password
from app.db.tenant_context import use_tenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.auth import LoginRequest
from app.services.settings_service import SettingsService
from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "C0rrect-Horse-Battery!"


@pytest.fixture(autouse=True)
def _limiter_disabled() -> Iterator[None]:
    from app.api.deps import limiter

    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    """Duck-typed Request -- `login` only reads `.client`/`.headers` for audit metadata."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:  # pragma: no cover
        pass


async def _seed_require_2fa(session: AsyncSession, tid: int, value: bool) -> None:
    """Write `auth.require_2fa` for a specific tenant via the tenant-bound writer."""
    async with use_tenant(tid):
        await SettingsService(session).set("auth.require_2fa", value)


async def _make_home_b_user(session: AsyncSession, b_tid: int) -> str:
    username = f"m2-{uuid.uuid4().hex}@local"
    user = AppUser(
        username=username,
        password_hash=hash_password(PASSWORD),
        role="admin",
        is_sso=False,
        tenant_id=b_tid,
    )
    session.add(user)
    await session.flush()
    return username


async def test_requirement_bites_in_the_accounts_home_tenant(session: AsyncSession) -> None:
    """Tenant B requires 2FA, default does not. A B-homed local account without 2FA set up
    MUST be funnelled into setup. Before the fix the login reads the default tenant (off)
    and hands out a full session instead -> red."""
    default_tid = (await tenant_repo.default_tenant(session)).id
    b = await tenant_repo.create(session, name="M2 Tenant B", slug=f"m2b-{uuid.uuid4().hex[:8]}")
    b_tid = b.id
    await _seed_require_2fa(session, default_tid, False)
    await _seed_require_2fa(session, b_tid, True)
    username = await _make_home_b_user(session, b_tid)

    body = LoginRequest(username=username, password=PASSWORD)
    resp = await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert resp.two_factor_setup_required is True


async def test_default_tenant_requirement_is_not_forced_on_other_tenants(
    session: AsyncSession,
) -> None:
    """Default tenant requires 2FA, tenant B does not. A B-homed local account without 2FA
    MUST log in normally (no setup funnel). Before the fix the login reads the default
    tenant (on) and forces setup on the B account -> red."""
    default_tid = (await tenant_repo.default_tenant(session)).id
    b = await tenant_repo.create(session, name="M2 Tenant B2", slug=f"m2b2-{uuid.uuid4().hex[:8]}")
    b_tid = b.id
    await _seed_require_2fa(session, default_tid, True)
    await _seed_require_2fa(session, b_tid, False)
    username = await _make_home_b_user(session, b_tid)

    body = LoginRequest(username=username, password=PASSWORD)
    resp = await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert resp.two_factor_setup_required is False
