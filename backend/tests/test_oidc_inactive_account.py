"""TDD for L9: the OIDC callback must reject a DISABLED existing SSO account.

The existing-account branch (`else:` in `oidc_callback`) updated `is_sso`/`role`, created a
`user_session` row and recorded a `LOGIN_SUCCESS` audit entry WITHOUT checking `is_active`.
The tokens are inert (get_current_user/refresh gate on `is_active`), but the login still left
an orphan session row and a misleading `LOGIN_SUCCESS` in the audit trail. This test drives
the callback for a matching, in-group but inactive SSO account and asserts it is denied like
the other SSO deny-paths: `sso_denied=1` redirect, a `LOGIN_FAILED` (reason `inactive`), NO
`LOGIN_SUCCESS`, and NO session row.

Seed pattern mirrors `test_oidc_group_auth.py`: the callback opens real second connections
(`tenant_scoped_session`, `read_mode`, `get_session_factory`), so everything must be really
committed. Single-tenant mode keeps the fixture minimal (settings-group authorization).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from app.api.deps import OIDC_FLOW_COOKIE
from app.api.routes.auth import oidc_callback
from app.core.security import hash_password
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.models.user import UserSession
from app.repositories import user_repo
from app.services import oidc
from app.services.audit import LOGIN_FAILED, LOGIN_SUCCESS
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

from tests.test_oidc_group_auth import _oidc_limiter_disabled  # noqa: F401

_USER_DOMAIN = "@inactive-sso.test"
_TID_DEFAULT = "inactive-sso-default-tid"
_SETTINGS_ADMIN_GROUP = "inactive-sso-settings-admins"


class _FakeRequest:
    """Duck-typed Request -- `oidc_callback`/`audit.record` read only these attributes."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


async def _call_oidc_callback(session: AsyncSession, result: oidc.OidcResult) -> RedirectResponse:
    request = _FakeRequest()
    request.cookies = {OIDC_FLOW_COOKIE: oidc.encode_flow_cookie({"state": "x", "nonce": "n"})}
    with patch("app.services.oidc.exchange_and_verify", new=AsyncMock(return_value=result)):
        resp = await oidc_callback(
            request,  # type: ignore[arg-type]
            session,
            code="fake-code",
            state="x",
            error=None,
        )
    assert isinstance(resp, RedirectResponse)
    return resp


class _Env:
    default_id: int


async def _write_mode(engine: AsyncEngine, default_id: int, value: bool) -> None:
    """Set the (default-tenant-scoped) multi-tenant mode, committed so the callback sees it
    over its own `read_mode` connection."""
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:tid, 'instance.multi_tenant_mode', to_jsonb(CAST(:val AS boolean)), "
                "false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = now()"
            ),
            {"tid": default_id, "val": value},
        )
        await conn.commit()


@pytest_asyncio.fixture
async def env(migrated_engine: AsyncEngine) -> AsyncGenerator[_Env]:
    async with migrated_engine.connect() as conn:
        default_id = (
            await conn.execute(text("SELECT id FROM tenant WHERE is_default"))
        ).scalar_one()
        orig_entra = (
            await conn.execute(
                text("SELECT entra_tenant_id FROM tenant WHERE id = :id"), {"id": default_id}
            )
        ).scalar_one()

        # Bind the default tenant to a known `tid` so the SSO login resolves onto it.
        await conn.execute(
            text("UPDATE tenant SET entra_tenant_id = :tid WHERE id = :id"),
            {"tid": _TID_DEFAULT, "id": default_id},
        )
        # Settings admin-group (single-tenant path uses it to authorize the login).
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:id, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"id": default_id, "g": _SETTINGS_ADMIN_GROUP},
        )
        await conn.commit()

        fx = _Env()
        fx.default_id = default_id
        try:
            yield fx
        finally:
            await conn.execute(
                text(f"DELETE FROM audit_log WHERE actor_username LIKE '%{_USER_DOMAIN}'")
            )
            await conn.execute(
                text(
                    "DELETE FROM user_session WHERE user_id IN "
                    f"(SELECT id FROM app_user WHERE username LIKE '%{_USER_DOMAIN}')"
                )
            )
            await conn.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_USER_DOMAIN}'"))
            await conn.execute(
                text(
                    "DELETE FROM setting WHERE tenant_id = :id AND key IN "
                    "('oidc.admin_group_id', 'instance.multi_tenant_mode')"
                ),
                {"id": default_id},
            )
            await conn.execute(
                text("UPDATE tenant SET entra_tenant_id = :orig WHERE id = :id"),
                {"orig": orig_entra, "id": default_id},
            )
            await conn.commit()


def _result(username: str) -> oidc.OidcResult:
    return oidc.OidcResult(
        username=username,
        display_name="X",
        allowed=True,  # recomputed authoritatively in the callback -- must not decide here
        role="admin",
        tid=_TID_DEFAULT,
        groups=[_SETTINGS_ADMIN_GROUP],
    )


async def test_inactive_existing_sso_account_denied(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, False)  # single-tenant path
    username = f"disabled{_USER_DOMAIN}"

    # Pre-existing SSO account that is disabled but still matches + is in the admin group.
    async with get_session_factory()() as session:
        user = await user_repo.create(
            session,
            username=username,
            password_hash=hash_password("x"),
            role="admin",
            is_sso=True,
            tenant_id=env.default_id,
        )
        user.is_active = False
        await session.commit()

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username))

    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None
        assert user.id is not None

        # No orphan session row must have been created.
        sessions = (
            (await session.execute(select(UserSession).where(UserSession.user_id == user.id)))
            .scalars()
            .all()
        )
        assert sessions == []

        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.actor_username == username)))
            .scalars()
            .all()
        )
        # A LOGIN_FAILED (reason inactive) is recorded; a LOGIN_SUCCESS must NOT be.
        assert any(
            r.action == LOGIN_FAILED
            and r.outcome == "failure"
            and r.detail.get("sso") is True
            and r.detail.get("reason") == "inactive"
            for r in rows
        )
        assert all(r.action != LOGIN_SUCCESS for r in rows)
