"""H4 core: dedicated non-superuser login role `pwnotify_runtime` for tenant-scoped sessions.

Task 2 replaces the single owner-superuser connection with TWO engines: the owner engine
(unchanged, used for owner-context work: migrations, startup housekeeping, audit trail on
NULL-tenant rows) and a new runtime engine that authenticates as `pwnotify_runtime` -- a
`LOGIN NOSUPERUSER NOBYPASSRLS` role, member of `pwnotify_app`. Only `tenant_scoped_session`
is routed through the runtime engine.

This suite proves the property that motivates the change: on the OLD single-engine design,
a tenant-scoped transaction ran on the superuser connection, so `SET LOCAL ROLE pwnotify_app`
could always be undone with `RESET ROLE` -- reverting to the superuser, which bypasses RLS by
table ownership. A real `pwnotify_runtime` connection cannot do that: `RESET ROLE` only falls
back to `pwnotify_runtime` itself, which is NOSUPERUSER/NOBYPASSRLS, so RLS still applies.

Seed pattern follows `test_isolation_attack.py`/`test_runtime_isolation.py`: two tenants seeded
via a real, committed connection on `migrated_engine` (a savepoint-isolated session is invisible
to the separate `pwnotify_runtime` connection used below), cleaned up in `finally`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest_asyncio.fixture
async def two_tenants_committed(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Two real tenants, each with one `setting` row, committed on a raw connection."""
    async with migrated_engine.connect() as conn:
        a, b = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('RuntimeRoleA','runtime-role-a',true,now()), "
                        "('RuntimeRoleB','runtime-role-b',true,now()) "
                        "RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:tid, 'probe', '\"a\"'::jsonb, false, now())"
            ),
            {"tid": a},
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:tid, 'probe', '\"b\"'::jsonb, false, now())"
            ),
            {"tid": b},
        )
        await conn.commit()
        try:
            yield {"a": int(a), "b": int(b)}
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


async def test_runtime_role_is_non_superuser_non_bypass_login(session) -> None:
    """`pwnotify_runtime` exists, can log in, and has neither superuser nor RLS-bypass power."""
    row = (
        await session.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                "WHERE rolname = 'pwnotify_runtime'"
            )
        )
    ).one_or_none()
    assert row is not None, "pwnotify_runtime role is missing"
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    assert row.rolcanlogin is True


async def test_runtime_role_is_member_of_app_role(session) -> None:
    """`GRANT pwnotify_app TO pwnotify_runtime` must hold, or `SET LOCAL ROLE pwnotify_app`
    inside a runtime-engine transaction would fail with insufficient privilege."""
    is_member = (
        await session.execute(
            text("SELECT pg_has_role('pwnotify_runtime', 'pwnotify_app', 'USAGE')")
        )
    ).scalar_one()
    assert is_member is True


async def test_runtime_connection_cannot_escape_rls_via_reset_role(
    two_tenants_committed: dict[str, int],
) -> None:
    """A real `pwnotify_runtime` connection, scoped to tenant A exactly like
    `apply_tenant_on_begin` does, sees only A's `setting` row -- and critically, `RESET ROLE`
    does NOT lift the isolation (unlike the old superuser-login design)."""
    from app.core.config import get_settings

    a, b = two_tenants_committed["a"], two_tenants_committed["b"]

    rt = create_async_engine(get_settings().runtime_database_url, future=True)
    try:
        async with rt.connect() as conn:
            await conn.begin()
            await conn.exec_driver_sql("SET LOCAL ROLE pwnotify_app")
            await conn.exec_driver_sql(f"SET LOCAL app.current_tenant = '{a}'")
            seen_a = set(
                (await conn.execute(text("SELECT tenant_id FROM setting"))).scalars().all()
            )
            assert a in seen_a
            assert b not in seen_a

            await conn.exec_driver_sql("RESET ROLE")
            after_reset = set(
                (await conn.execute(text("SELECT tenant_id FROM setting"))).scalars().all()
            )
            assert b not in after_reset, (
                "RESET ROLE escaped tenant isolation -- runtime connection is still privileged"
            )
    finally:
        await rt.dispose()
