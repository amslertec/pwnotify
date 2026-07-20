"""M8 + L8: the `sync_sso_users` UPDATE branch must be tenant-scoped and must not force-reactivate.

Two latent flaws in the ``else`` (existing-SSO-account) branch of ``oidc.sync_sso_users``:

* **M8** -- the branch overwrote ``role``/``display_name``/``is_active`` of an existing SSO
  account found instance-wide via ``get_by_username``, WITHOUT checking that the account is
  homed in the tenant being synced. A customer admin controlling their own tenant's Entra
  settings could steer a same-UPN account homed in ANOTHER tenant: overwrite its role and
  revive it. Only the removal pass was tenant-scoped (``list_sso_for_tenant``); the update
  pass was not.
* **L8** -- the branch set ``is_active = True`` on EVERY sync. Latent today (no endpoint
  deactivates an account yet), but the moment one exists a deliberately deactivated account
  would be silently revived on the next group sync.

Both tests seed real accounts on real Postgres (port 5433, see ``conftest.py``) and mock
Microsoft Graph (``patch.object(oidc, "GraphClient", ...)``, pattern from
``test_sso_sync_tenant_scope.py``). Against the pre-fix code:
  * M8 fails -- tenant A's admin account is overwritten to auditor / reactivated by B's sync.
  * L8 fails -- the deactivated account is flipped back to active.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from app.db.session import get_session_factory
from app.repositories import user_repo
from app.services import oidc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_DOMAIN = "@ssoupd.test"

SETTINGS: dict[str, Any] = {
    "oidc.enabled": True,
    "oidc.admin_group_id": "admin-gruppe",
    "oidc.auditor_group_id": "",
    "graph.tenant_id": "t",
    "graph.client_id": "c",
    "graph.client_secret": "s",
    "graph.cloud": "global",
}


class _TwoTenants:
    a: int
    b: int


@pytest_asyncio.fixture
async def two_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[_TwoTenants]:
    """Two active tenants A and B, cleaned up by UPN domain afterwards."""
    async with migrated_engine.connect() as conn:
        a = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('SsoUpdTenantA','ssoupd-tenant-a',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        b = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('SsoUpdTenantB','ssoupd-tenant-b',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()

    tenants = _TwoTenants()
    tenants.a, tenants.b = a, b
    try:
        yield tenants
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_DOMAIN}'"))
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


def _graph_returning(upn: str, display: str) -> MagicMock:
    """Fake Graph whose admin group has exactly one member (`upn`)."""

    async def _get_group_members(group_id: str) -> list[dict[str, Any]]:
        if group_id == "admin-gruppe":
            return [{"userPrincipalName": upn, "displayName": display}]
        return []

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_get_group_members)
    fake.aclose = AsyncMock()
    return fake


async def test_sync_for_tenant_b_never_overwrites_tenant_a_sso_account(
    two_tenants: _TwoTenants,
) -> None:
    """M8: a same-UPN account homed in tenant A must be untouched by tenant B's sync."""
    upn = f"shared-auditor{_DOMAIN}"
    async with get_session_factory()() as session:
        await user_repo.create(
            session,
            username=upn,
            password_hash="x",
            role="auditor",  # A's account is a mere auditor
            display_name="A User",
            is_sso=True,
            tenant_id=two_tenants.a,
        )

    # B's admin group lists the SAME upn. A pre-fix sync would promote it to admin (privilege
    # escalation across tenants) and overwrite its display name.
    with patch.object(oidc, "GraphClient", return_value=_graph_returning(upn, "Impostor")):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, SETTINGS, tenant_id=two_tenants.b)

    async with get_session_factory()() as session:
        acct = await user_repo.get_by_username(session, upn)
    assert acct is not None, "A's SSO account was deleted by B's sync!"
    assert acct.tenant_id == two_tenants.a, "home tenant must stay A"
    assert acct.role == "auditor", "B's sync must not promote A's account role"
    assert acct.display_name == "A User", "B's sync must not overwrite A's display name"
    assert acct.is_active is True
    # foreign-tenant conflict is skipped, not counted as synced
    assert stats["synced"] == 0


async def test_sync_does_not_force_reactivate_deactivated_account(
    two_tenants: _TwoTenants,
) -> None:
    """L8: a deliberately deactivated SSO account must stay inactive across a sync."""
    upn = f"dormant-admin{_DOMAIN}"
    async with get_session_factory()() as session:
        user = await user_repo.create(
            session,
            username=upn,
            password_hash="x",
            role="admin",
            display_name="Dormant",
            is_sso=True,
            tenant_id=two_tenants.a,
        )
        # simulate a future deactivation path
        user.is_active = False
        session.add(user)
        await session.commit()

    with patch.object(oidc, "GraphClient", return_value=_graph_returning(upn, "Dormant")):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, SETTINGS, tenant_id=two_tenants.a)

    async with get_session_factory()() as session:
        acct = await user_repo.get_by_username(session, upn)
    assert acct is not None
    assert acct.is_active is False, "sync must not force-reactivate a deactivated account"
    # in-tenant account still present in desired -> not removed, counted as synced
    assert stats["synced"] == 1
    assert stats["removed"] == 0
