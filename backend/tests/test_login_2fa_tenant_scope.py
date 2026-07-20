"""F-03 (was M2): the login handler must read `auth.require_2fa` from the account's HOME
tenant, tenant-scoped -- not from the owner session's default-tenant fallback.

`auth.require_2fa` is a regular PER-TENANT setting (no `instance.` prefix in
`settings_schema.py`): every customer governs its own 2FA requirement via `PUT /settings`.
Reading it on the owner session (no tenant context) would fall back to the DEFAULT tenant
and apply the wrong customer's value.

The fix reads the requirement through `tenant_scoped_session(home_tenant)` (a SEPARATE
runtime connection, RLS-enforced) instead of setting the tenant `ContextVar` around the
owner session -- so the owner session's later writes in `_complete_login` never risk running
under the `pwnotify_app` role. Because that read runs on its own connection, the per-tenant
settings under test must be REALLY committed (savepoint-only data on the owner session would
be invisible to it) -- hence the committing `tenant_env` fixture below (pattern from
`tests/test_oidc_role_per_tenant.py`).

Consequences proven below:

1. Tenant B requires 2FA, default does not -> a B-homed local account is funnelled into setup.
2. Default requires 2FA, tenant B does not -> a B-homed account logs in normally.
3. An instance-wide local admin (`tenant_id is None`) resolves to the DEFAULT tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator
from typing import Any

import pytest
import pytest_asyncio
from app.api.routes.auth import login
from app.core.security import hash_password
from app.models.user import AppUser
from app.schemas.auth import LoginRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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


class _TenantEnv:
    """Committed test data for the tenant-scoped read: a factory for fresh tenants and a
    per-tenant `auth.require_2fa` setter. Everything it commits is torn down afterwards."""

    default_tid: int
    make_tenant: Callable[[str], Coroutine[Any, Any, int]]
    set_require_2fa: Callable[[int, bool], Coroutine[Any, Any, None]]


@pytest_asyncio.fixture
async def tenant_env(migrated_engine: AsyncEngine) -> AsyncGenerator[_TenantEnv]:
    """`tenant_scoped_session` opens a SEPARATE connection, so per-tenant settings under test
    must be really committed (not just savepoint-visible on the owner session). Tracks and
    cleans up every tenant/setting it commits."""
    created_tenants: list[int] = []
    seeded_settings: list[tuple[int, str]] = []

    async with migrated_engine.connect() as conn:

        async def make_tenant(prefix: str) -> int:
            tid = (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES (:n, :s, true, now()) RETURNING id"
                    ),
                    {"n": prefix, "s": f"{prefix}-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            created_tenants.append(tid)
            await conn.commit()
            return int(tid)

        async def set_require_2fa(tid: int, value: bool) -> None:
            await conn.execute(
                text(
                    "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                    "VALUES (:t, 'auth.require_2fa', to_jsonb(CAST(:v AS boolean)), false, now()) "
                    "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"t": tid, "v": value},
            )
            seeded_settings.append((tid, "auth.require_2fa"))
            await conn.commit()

        env = _TenantEnv()
        env.default_tid = int(
            (await conn.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        )
        env.make_tenant = make_tenant
        env.set_require_2fa = set_require_2fa
        try:
            yield env
        finally:
            for tid, key in seeded_settings:
                await conn.execute(
                    text("DELETE FROM setting WHERE tenant_id = :t AND key = :k"),
                    {"t": tid, "k": key},
                )
            for tid in created_tenants:
                # tenant delete cascades onto its own `setting` rows.
                await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
            await conn.commit()


async def _make_local_user(session: AsyncSession, tenant_id: int | None) -> str:
    """Local (non-SSO) account homed in `tenant_id`. Created on the owner session (rolled back
    with the fixture); `login` reads it from this same session, so no commit is needed here."""
    username = f"f03-{uuid.uuid4().hex}@local"
    user = AppUser(
        username=username,
        password_hash=hash_password(PASSWORD),
        role="admin",
        is_sso=False,
        tenant_id=tenant_id,
    )
    session.add(user)
    await session.flush()
    return username


async def test_requirement_bites_in_the_accounts_home_tenant(
    tenant_env: _TenantEnv, session: AsyncSession
) -> None:
    """Tenant B requires 2FA, default does not. A B-homed local account without 2FA set up
    MUST be funnelled into setup -- the requirement is read from its HOME tenant."""
    b_tid = await tenant_env.make_tenant("f03b")
    await tenant_env.set_require_2fa(b_tid, True)
    username = await _make_local_user(session, b_tid)

    body = LoginRequest(username=username, password=PASSWORD)
    resp = await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert resp.two_factor_setup_required is True


async def test_default_tenant_requirement_is_not_forced_on_other_tenants(
    tenant_env: _TenantEnv, session: AsyncSession
) -> None:
    """Default tenant requires 2FA, tenant B does not. A B-homed local account without 2FA
    MUST log in normally -- the default's value must not leak onto tenant B."""
    b_tid = await tenant_env.make_tenant("f03b2")
    await tenant_env.set_require_2fa(tenant_env.default_tid, True)
    await tenant_env.set_require_2fa(b_tid, False)
    username = await _make_local_user(session, b_tid)

    body = LoginRequest(username=username, password=PASSWORD)
    resp = await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert resp.two_factor_setup_required is False


async def test_instance_wide_admin_resolves_to_default_tenant(
    tenant_env: _TenantEnv, session: AsyncSession
) -> None:
    """An instance-wide local admin has `tenant_id is None`; the requirement must then be read
    from the DEFAULT tenant (its home). Default requires 2FA -> funnelled into setup."""
    await tenant_env.set_require_2fa(tenant_env.default_tid, True)
    username = await _make_local_user(session, None)

    body = LoginRequest(username=username, password=PASSWORD)
    resp = await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert resp.two_factor_setup_required is True
