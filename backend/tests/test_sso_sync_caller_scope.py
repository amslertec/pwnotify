"""H2: `POST /admin/users/sso/sync` must be tenant-scoped for a non-superadmin caller (only THEIR
tenant is synced) and must NOT leak foreign tenant names in the response. Before the fix it looped
over ALL active tenants and appended each blocked tenant's `.name` into the message.

Wiring-level test like `test_sync_sso_tenant_scope.py`: `oidc.sync_sso_users` is monkeypatched to
capture which `tenant_id`s it is called with. A signed access token carries the caller's
`active_tenant` claim; the caller is a local admin granted admin access to tenant A only. Tenant B
(also active, also oidc-configured) must NOT be synced, and its name must not appear anywhere in
the response."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE
from app.api.routes.admin_users import sync_sso
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.repositories import tenant_repo, user_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_B_NAME = "H2CustomerB-SecretName"


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


@pytest_asyncio.fixture
async def two_configured_tenants_and_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int, int]]:
    """Tenants A and B, both active and oidc-configured; a local admin granted admin access to A.
    Yields (a_id, b_id, admin_id)."""
    async with migrated_engine.connect() as conn:
        a = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('H2CustomerA','h2-customer-a',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        b = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "(:bname,'h2-customer-b',true,now()) RETURNING id"
                    ),
                    {"bname": _B_NAME},
                )
            ).scalar_one()
        )
        for tid in (a, b):
            await conn.execute(
                text(
                    "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                    "(:t, 'oidc.enabled', to_jsonb(true), false, now()), "
                    "(:t, 'oidc.admin_group_id', to_jsonb('grp'::text), false, now())"
                ),
                {"t": tid},
            )
        await conn.commit()

    factory = get_session_factory()
    async with factory() as s:
        admin = await user_repo.create(
            s,
            username=f"h2-admin-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="admin",
            is_sso=False,
        )
        await tenant_repo.add_grant(s, user_id=admin.id, tenant_id=a, kind="admin")
    try:
        yield a, b, int(admin.id)
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :a"), {"a": a})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": admin.id})
            await conn.execute(
                text("DELETE FROM setting WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
            )
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


async def test_tenant_admin_sync_scoped_and_no_name_leak(
    two_configured_tenants_and_admin: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    a_id, b_id, admin_id = two_configured_tenants_and_admin
    seen_tenant_ids: list[int] = []

    async def _fake_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        seen_tenant_ids.append(tenant_id)
        # Force the "removal blocked" branch to prove the message reports a COUNT, not a name.
        return {"synced": 1, "removed": 0, "removal_blocked": 1}

    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sync)

    factory = get_session_factory()
    async with factory() as session:
        admin = await user_repo.get(session, admin_id)
        assert admin is not None
        request = _FakeRequest(
            {ACCESS_COOKIE: issue_token_pair(str(admin_id), active_tenant=a_id).access_token}
        )
        msg = await sync_sso(request, admin, session)  # type: ignore[arg-type]

    assert seen_tenant_ids == [a_id], f"sync must be scoped to tenant A only: {seen_tenant_ids}"
    assert str(b_id) not in msg.message
    assert _B_NAME not in msg.message, "foreign tenant name leaked into the response"
