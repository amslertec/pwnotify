"""Task 1 (Phase 5, M2): `SettingsService.get_all` ran `select(Setting)` with no tenant
predicate. On an owner session (no active tenant, RLS bypassed by ownership) this folded
EVERY tenant's settings into one dict (last-wins on the shared `key`) and decrypted every
tenant's secrets along the way -- a cross-tenant disclosure on every owner-session caller
(`version.py`, `setup.py`, `auth.auth_config`).

The fix adds an explicit tenant filter: the active tenant if one is bound, otherwise the
default tenant (owner session, no context). This proves the fold is gone by seeding the
SAME key with two different values on two different tenants and asserting the owner-session
read returns only the default tenant's value -- before the fix the result is non-deterministic
(whichever row `select(Setting)` happens to return last wins), so the assertion below is red
without the fix.

Seed pattern like `test_audit_tenant_scope.py`: real, committed superuser connection
(RLS-free) for setup -- `tenant_scoped_session` opens its own runtime connection and would
not see uncommitted data from the savepoint-isolated `session` fixture.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest_asyncio
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.services.settings_service import SettingsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def owner_scope_seed(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Default tenant (already present) + a second tenant `S5B`, each with its own
    `branding.app_name` setting row, so the two are trivially distinguishable."""
    async with migrated_engine.connect() as conn:
        default_tid = int(
            (
                await conn.execute(text("SELECT id FROM tenant WHERE is_default IS TRUE"))
            ).scalar_one()
        )
        b_tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES ('S5B', 's5-b', true, now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:default_tid, 'branding.app_name', :default_val, false, now()), "
                "(:b_tid, 'branding.app_name', :b_val, false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {
                "default_tid": default_tid,
                "b_tid": b_tid,
                "default_val": json.dumps("DefaultCo"),
                "b_val": json.dumps("OtherCo"),
            },
        )
        await conn.commit()
        try:
            yield {"default": default_tid, "b": b_tid}
        finally:
            await conn.execute(
                text(
                    "DELETE FROM setting WHERE tenant_id IN (:default_tid, :b_tid) "
                    "AND key = 'branding.app_name'"
                ),
                {"default_tid": default_tid, "b_tid": b_tid},
            )
            await conn.execute(text("DELETE FROM tenant WHERE id = :b_tid"), {"b_tid": b_tid})
            await conn.commit()


async def test_owner_session_get_all_scopes_to_default_tenant_not_folded(
    owner_scope_seed: dict[str, int],
) -> None:
    """Owner session, no active tenant context: `get_all()` must return ONLY the default
    tenant's `branding.app_name` ('DefaultCo'), never the folded/other-tenant value
    ('OtherCo'). Before the fix this is not deterministic across tenants."""
    async with get_session_factory()() as owner:
        result = await SettingsService(owner).get_all()
    leaked = result["branding.app_name"]
    assert leaked == "DefaultCo", (
        f"Owner session leaked/folded a non-default tenant's setting: {leaked!r}"
    )


async def test_tenant_scoped_session_get_all_still_returns_own_tenant(
    owner_scope_seed: dict[str, int],
) -> None:
    """Sanity check for the untouched path: inside `tenant_scoped_session(b_tid)`,
    `get_all()` returns B's own value ('OtherCo') -- RLS and the explicit filter stay
    consistent with each other."""
    b_tid = owner_scope_seed["b"]
    async with tenant_scoped_session(b_tid) as session:
        result = await SettingsService(session).get_all()
    assert result["branding.app_name"] == "OtherCo"
