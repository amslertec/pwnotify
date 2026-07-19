"""H3: an SSO code path must NEVER adopt an existing non-SSO (local) account -- especially a local
superadmin. Before the fix, `oidc_callback` and `oidc.sync_sso_users` flipped a same-named local
account to `is_sso=True` and overwrote its role, which locks the real superadmin out
(`require_superadmin` needs `not is_sso`) and hands an Entra identity the account.

- `oidc_callback`: an SSO login whose UPN matches a local account is DENIED (redirect
  `sso_denied=1`, `LOGIN_FAILED` audited) and the account is left untouched.
- `sync_sso_users`: a same-UPN local account is SKIPPED (not flipped, not counted).

oidc_callback seeding mirrors `test_oidc_role_per_tenant.py` (a tenant with `entra_tenant_id` +
admin group so the login would otherwise succeed -- proving the denial is the new guard).
sync_sso_users mirrors `test_sso_sync_tenant_scope.py` (mocked Graph)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from app.api.routes.auth import oidc_callback
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.repositories import user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

_DOMAIN = "@h3lockout.test"
_TID = "h3-lockout-entra-tid"
_ADMIN_GROUP = "h3-lockout-admin-group"


class _FakeRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


async def _call_callback(session: AsyncSession, result: oidc.OidcResult) -> RedirectResponse:
    request = _FakeRequest()
    state = oidc.sign_state()
    with patch("app.services.oidc.exchange_and_verify", new=AsyncMock(return_value=result)):
        resp = await oidc_callback(
            request,
            session,
            code="fake-code",
            state=state,
            error=None,  # type: ignore[arg-type]
        )
    assert isinstance(resp, RedirectResponse)
    return resp


@pytest_asyncio.fixture
async def tenant_bound_to_entra(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """One active tenant bound to _TID with an admin group configured -- so an SSO login would
    normally succeed (proving the denial below is the local-account guard, not a config gap)."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                        "VALUES ('H3Tenant','h3-tenant',:e,true,now()) RETURNING id"
                    ),
                    {"e": _TID},
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:t, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now())"
            ),
            {"t": tid, "g": _ADMIN_GROUP},
        )
        await conn.commit()
        try:
            yield tid
        finally:
            async with migrated_engine.connect() as c2:
                # Pre-fix, a matching login completes and issues a session for the impostor --
                # delete it before app_user (FK) so cleanup stays robust in both RED and GREEN.
                await c2.execute(
                    text(
                        "DELETE FROM user_session WHERE user_id IN "
                        f"(SELECT id FROM app_user WHERE username LIKE '%{_DOMAIN}')"
                    )
                )
                await c2.execute(
                    text(f"DELETE FROM audit_log WHERE actor_username LIKE '%{_DOMAIN}'")
                )
                await c2.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_DOMAIN}'"))
                await c2.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tid})
                await c2.commit()


async def _seed_local(role: str) -> str:
    username = f"{role}-{uuid.uuid4().hex[:6]}{_DOMAIN}"
    async with get_session_factory()() as session:
        await user_repo.create(
            session, username=username, password_hash="x", role=role, is_sso=False
        )
    return username


async def test_oidc_callback_does_not_adopt_local_superadmin(
    tenant_bound_to_entra: int,
) -> None:
    username = await _seed_local("superadmin")
    result = oidc.OidcResult(
        username=username,
        display_name="Impostor",
        allowed=True,
        role="admin",
        tid=_TID,
        groups=[_ADMIN_GROUP],
    )
    async with get_session_factory()() as session:
        resp = await _call_callback(session, result)

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.is_sso is False, "local superadmin must not be flipped to SSO"
        assert user.role == "superadmin", "role must be unchanged"

        audit_row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert audit_row.action == LOGIN_FAILED
        assert audit_row.outcome == "failure"


async def test_oidc_callback_does_not_adopt_local_admin(tenant_bound_to_entra: int) -> None:
    username = await _seed_local("admin")
    result = oidc.OidcResult(
        username=username,
        display_name="Impostor",
        allowed=True,
        role="admin",
        tid=_TID,
        groups=[_ADMIN_GROUP],
    )
    async with get_session_factory()() as session:
        resp = await _call_callback(session, result)
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.is_sso is False


async def test_sync_sso_users_skips_local_account(tenant_bound_to_entra: int) -> None:
    """`sync_sso_users` must skip a same-UPN local account (not flip, not count).

    Requests `tenant_bound_to_entra` solely so its `finally` cleanup (LIKE '%@h3lockout.test')
    removes the local account seeded via `_seed_local` below -- this test has no other cleanup."""
    username = await _seed_local("admin")
    settings: dict[str, Any] = {
        "oidc.enabled": True,
        "oidc.admin_group_id": "grp",
        "oidc.auditor_group_id": "",
        "graph.tenant_id": "t",
        "graph.client_id": "c",
        "graph.client_secret": "s",
        "graph.cloud": "global",
    }

    async def _members(group_id: str) -> list[dict[str, Any]]:
        return [{"userPrincipalName": username, "displayName": "Impostor"}]

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_members)
    fake.aclose = AsyncMock()

    # A tenant id to scope to -- any active tenant works; use the default.
    async with get_session_factory()() as session:
        default_id = int(
            (await session.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        )

    with patch.object(oidc, "GraphClient", return_value=fake):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, settings, tenant_id=default_id)

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.is_sso is False, "local account must not be adopted by the SSO sync"
        assert user.role == "admin"
    assert stats["synced"] == 0, "a skipped local account must not be counted as synced"
