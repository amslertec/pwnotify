"""TDD for Task 4 (H8): write-scoped tenant authorization + `set_role` grant migration.

`_resolve_authorized_tenant` (`deps.py`) previously ALWAYS authorized with the read gate
(`tenant_repo.is_allowed(..., write=False)`), even for the tenant-data WRITE/action routes
(`TenantSessionDep`/`TenantSettingsDep` are shared by read and write routes alike). An
account whose `AppUser.role` is `"admin"` (passes the `AdminUser` role gate) but whose only
tenant grant row is `auditor_tenant` (e.g. a stale grant left behind by a `set_role`
auditor->admin promotion, Minor-1 from the Phase-1 review) could therefore trigger a run or
mutate settings/users on a tenant it should only be able to READ.

This suite proves, non-vacuously:
1. Such a stale-grant account is rejected (`tenant_forbidden`) by the WRITE gate on both
   `/runs/trigger` and a `settings` write route, while a READ route (`list_runs`) still
   succeeds for the very same account (auditor grant still permits read).
2. `set_role` now migrates the grant row itself (auditor_tenant <-> admin_tenant) so the
   capability actually matches the new role, in both directions.

Route functions are driven directly (pattern from `test_matrix_b_route_gating.py` /
`test_route_tenant_scoping.py`): a real signed access token carries the `active_tenant`
claim, `get_current_user` resolves the caller on an owner session, then the (write-scoped)
tenant session dependency is driven exactly like FastAPI would drive it.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.api.deps import (
    ACCESS_COOKIE,
    get_current_user,
    get_tenant_session,
    get_tenant_session_write,
    get_tenant_settings_service_write,
)
from app.api.routes.admin_users import set_role
from app.api.routes.runs import TriggerRequest, list_runs, trigger
from app.api.routes.settings import template_reset
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.models.tenant import AdminTenant, AuditorTenant
from app.models.user import AppUser
from app.repositories import tenant_repo
from app.schemas.auth import RoleUpdate
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


class _FakeRequest:
    """Duck-typed Request -- the guards/routes exercised here only read `.cookies`."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


def _slug() -> str:
    return f"wsta-{uuid.uuid4().hex[:10]}"


@contextlib.asynccontextmanager
async def _tenant_read_session_for(uid: int, *, claim: int | None):
    """Drives `get_tenant_session` (READ gate) exactly like FastAPI would per request.
    Yields `(user, scoped_session)` -- callers need the resolved `AppUser` for route
    signatures like `list_runs(_: CurrentUser, ...)`; a fresh lookup on the tenant-scoped
    (app-role) session would fail (no SELECT on `app_user` for `pwnotify_app`, see
    migration `f7a8b9c0d1e2`)."""
    pair = issue_token_pair(str(uid), active_tenant=claim)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session(request, user, owner)
        try:
            yield user, await anext(gen)
        finally:
            await gen.aclose()


@contextlib.asynccontextmanager
async def _tenant_write_settings_for(uid: int, *, claim: int | None):
    """Drives `get_tenant_session_write` -> `get_tenant_settings_service_write` (WRITE gate)
    exactly like FastAPI would build `TenantWriteSettingsDep` per request."""
    pair = issue_token_pair(str(uid), active_tenant=claim)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session_write(request, user, owner)
        try:
            scoped = await anext(gen)
            svc = await get_tenant_settings_service_write(scoped)
            yield svc
        finally:
            await gen.aclose()


@contextlib.asynccontextmanager
async def _owner_request_for(uid: int, *, claim: int | None):
    """Owner (un-scoped) session + real request/user -- for routes like `/runs/trigger` that
    resolve `_resolve_authorized_tenant` themselves instead of via a session dependency."""
    pair = issue_token_pair(str(uid), active_tenant=claim)
    request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        yield request, user, owner


@pytest_asyncio.fixture
async def stale_grant_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int]]:
    """A committed local `role=="admin"` account holding ONLY an `auditor_tenant` grant on a
    freshly created tenant -- simulates the Minor-1 stale-grant state directly (bypassing
    `set_role`) so the WRITE gate itself is proven, independent of the grant-migration fix
    below. `migrated_engine` (own connection, real commit) because `get_tenant_session*`
    open their own connection via `get_session_factory()` and would not see uncommitted rows
    from the savepoint-isolated `session` fixture (same pattern as
    `test_active_tenant_resolution.py`)."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "(:name, :slug, true, now()) RETURNING id"
                    ),
                    {"name": "Wsta Stale", "slug": _slug()},
                )
            ).scalar_one()
        )
        uid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user "
                        "(username, password_hash, role, is_active, is_sso, "
                        "failed_login_count, language, created_at, updated_at) VALUES "
                        "(:username, 'x', 'admin', true, false, 0, 'de', now(), now()) "
                        "RETURNING id"
                    ),
                    {"username": f"wsta-stale-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO auditor_tenant (user_id, tenant_id, source) VALUES "
                "(:uid, :tid, 'manual')"
            ),
            {"uid": uid, "tid": tid},
        )
        await conn.commit()
        try:
            yield uid, tid
        finally:
            await conn.execute(
                text("DELETE FROM auditor_tenant WHERE user_id = :uid"), {"uid": uid}
            )
            await conn.execute(text("DELETE FROM app_user WHERE id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


# ---- 1. Stale auditor_tenant grant on a role=='admin' account: write blocked, read allowed --- #


async def test_stale_grant_admin_blocked_from_runs_trigger(
    stale_grant_admin: tuple[int, int],
) -> None:
    uid, tid = stale_grant_admin
    async with _owner_request_for(uid, claim=tid) as (request, user, session):
        with pytest.raises(ForbiddenError) as exc_info:
            await trigger(request, TriggerRequest(dry_run=True), user, session)  # type: ignore[arg-type]
        assert exc_info.value.code == "tenant_forbidden"


async def test_stale_grant_admin_blocked_from_settings_write_route(
    stale_grant_admin: tuple[int, int],
) -> None:
    uid, tid = stale_grant_admin
    with pytest.raises(ForbiddenError) as exc_info:
        async with _tenant_write_settings_for(uid, claim=tid) as svc:
            # The WRITE gate raises ForbiddenError on context entry above, so this call
            # never actually runs -- but keep its arity in step with the route signature.
            await template_reset(None, None, svc, None)  # type: ignore[arg-type]
    assert exc_info.value.code == "tenant_forbidden"


async def test_stale_grant_admin_still_allowed_on_read_route(
    stale_grant_admin: tuple[int, int],
) -> None:
    """Regression: the auditor grant still permits READ -- only the WRITE gate tightened."""
    uid, tid = stale_grant_admin
    async with _tenant_read_session_for(uid, claim=tid) as (user, session):
        page = await list_runs(user, session, page=1, page_size=25)  # type: ignore[arg-type]
        assert page.total == 0


# ---- 2. `set_role` migrates grant rows so capability matches the new role -------------------- #


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"wsta-superadmin-{uuid.uuid4().hex[:8]}", password_hash="x", role="superadmin"
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_local(session: AsyncSession, *, role: str) -> AppUser:
    user = AppUser(
        username=f"wsta-{role}-{uuid.uuid4().hex[:8]}", password_hash="x", role=role, is_sso=False
    )
    session.add(user)
    await session.flush()
    return user


async def test_set_role_auditor_to_admin_migrates_grant(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session)
    a = await tenant_repo.create(session, name="Wsta Migrate A", slug=_slug())
    assert a.id is not None
    target = await _mk_local(session, role="auditor")
    assert target.id is not None
    await tenant_repo.add_grant(session, user_id=target.id, tenant_id=a.id, kind="auditor")

    out = await set_role(None, caller, target.id, RoleUpdate(role="admin"), session)  # type: ignore[arg-type]
    assert out.role == "admin"

    admin_row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == target.id, AdminTenant.tenant_id == a.id
            )
        )
    ).scalar_one_or_none()
    assert admin_row is not None, "admin_tenant grant was not created by set_role"
    auditor_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == target.id, AuditorTenant.tenant_id == a.id
            )
        )
    ).scalar_one_or_none()
    assert auditor_row is None, "stale auditor_tenant grant was not removed by set_role"

    refreshed = await session.get(AppUser, target.id)
    assert refreshed is not None
    assert await tenant_repo.is_allowed(session, refreshed, a.id, write=True) is True


async def test_set_role_admin_to_auditor_migrates_grant_back(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session)
    a = await tenant_repo.create(session, name="Wsta Migrate B", slug=_slug())
    assert a.id is not None
    # A second admin so the last-admin-demotion guard does not block this test.
    await _mk_local(session, role="admin")
    target = await _mk_local(session, role="admin")
    assert target.id is not None
    await tenant_repo.add_grant(session, user_id=target.id, tenant_id=a.id, kind="admin")

    out = await set_role(None, caller, target.id, RoleUpdate(role="auditor"), session)  # type: ignore[arg-type]
    assert out.role == "auditor"

    auditor_row = (
        await session.execute(
            select(AuditorTenant).where(
                AuditorTenant.user_id == target.id, AuditorTenant.tenant_id == a.id
            )
        )
    ).scalar_one_or_none()
    assert auditor_row is not None, "auditor_tenant grant was not created by set_role"
    admin_row = (
        await session.execute(
            select(AdminTenant).where(
                AdminTenant.user_id == target.id, AdminTenant.tenant_id == a.id
            )
        )
    ).scalar_one_or_none()
    assert admin_row is None, "stale admin_tenant grant was not removed by set_role"

    refreshed = await session.get(AppUser, target.id)
    assert refreshed is not None
    assert await tenant_repo.is_allowed(session, refreshed, a.id, write=True) is False
    assert await tenant_repo.is_allowed(session, refreshed, a.id, write=False) is True
