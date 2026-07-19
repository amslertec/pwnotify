"""H1: `POST /runs/trigger` must be tenant-scoped for a non-superadmin caller (only THEIR active
tenant runs, and THEIR run is returned) while instance-wide fan-out stays superadmin-exclusive.

Before the fix the route called `trigger_now()` unconditionally, which loops over ALL active
tenants and returns the LAST tenant's run -- so any tenant-admin caused runs for every tenant and
received a foreign run object.

Real-commit pattern like `test_scheduler_tenant_scope.py`: the scheduler opens its own sessions
(`get_session_factory()`), so the caller is seeded on a real owner session and cleaned up in
`finally`. Heavy dependencies (Graph/mail/SSO sync/alert) are patched out -- only the wiring
(which tenants get a run, which run is returned) is under test. A signed access token in the fake
request carries the `active_tenant` claim that `_resolve_authorized_tenant` reads."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE
from app.api.routes.runs import TriggerRequest, trigger
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.db.tenant_context import open_active_session
from app.repositories import tenant_repo, user_repo
from app.services.scheduler import SchedulerService, set_scheduler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


async def _default_tenant_id(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        )


def _patch_heavy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sync_users(session: Any, settings: dict[str, Any]) -> dict[str, int]:
        return {"checked": 0}

    async def _fake_sso_sync(
        session: Any, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        return {"synced": 0, "removed": 0}

    async def _no_excluded(session: Any, settings: dict[str, Any]) -> set[str]:
        return set()

    async def _no_alert(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.services.runner.sync_users", _fake_sync_users)
    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr("app.services.runner._resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)


@pytest_asyncio.fixture
async def customer_and_admin(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, int, int]]:
    """A second active customer tenant C, and a local admin granted admin access to C.
    Yields (customer_id, admin_id, default_id)."""
    async with migrated_engine.connect() as conn:
        cid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('H1Customer','h1-customer',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()

    factory = get_session_factory()
    async with factory() as s:
        admin = await user_repo.create(
            s,
            username=f"h1-admin-{uuid.uuid4().hex[:8]}",
            password_hash="x",
            role="admin",
            is_sso=False,
        )
        await tenant_repo.add_grant(s, user_id=admin.id, tenant_id=cid, kind="admin")
    default_id = await _default_tenant_id(migrated_engine)
    try:
        yield cid, int(admin.id), default_id
    finally:
        async with migrated_engine.connect() as conn:
            # `run.triggered` audit rows (Security Phase 5, Task 8/M10) reference this
            # tenant via a plain FK (no ON DELETE) -- clear them before the tenant row.
            await conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM run WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM admin_tenant WHERE tenant_id = :c"), {"c": cid})
            await conn.execute(text("DELETE FROM user_session WHERE user_id = :u"), {"u": admin.id})
            await conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": admin.id})
            await conn.execute(text("DELETE FROM tenant WHERE id = :c"), {"c": cid})
            await conn.commit()


async def test_tenant_admin_trigger_is_scoped_to_own_tenant(
    customer_and_admin: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, admin_id, default_id = customer_and_admin
    _patch_heavy(monkeypatch)
    set_scheduler(SchedulerService(open_active_session, base_url="http://test.local"))

    factory = get_session_factory()

    # Baseline: highest default-tenant run id BEFORE the scoped trigger runs, read on its own
    # short-lived session so it cannot see any run the trigger call below is about to create.
    async with factory() as session:
        baseline_default_run_id = (
            await session.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM run WHERE tenant_id = :d"),
                {"d": default_id},
            )
        ).scalar_one()

    async with factory() as session:
        admin = await user_repo.get(session, admin_id)
        assert admin is not None
        request = _FakeRequest(
            {ACCESS_COOKIE: issue_token_pair(str(admin_id), active_tenant=cid).access_token}
        )
        run = await trigger(request, TriggerRequest(dry_run=True), admin, session)  # type: ignore[arg-type]

    # `RunDetail` (the route's response schema) does not expose `tenant_id`, so verify the
    # scoping directly against the row the route just created.
    async with factory() as session:
        run_tenant_id = (
            await session.execute(text("SELECT tenant_id FROM run WHERE id = :r"), {"r": run.id})
        ).scalar_one()
    assert run_tenant_id == cid, f"scoped trigger returned a foreign run: {run_tenant_id}"

    # No new run was created for the default tenant by this scoped call. Compared against the
    # baseline run id (not a time window) so this is deterministic regardless of what sibling
    # tests in this module do concurrently or within the same clock second.
    async with factory() as session:
        default_runs_for_this = (
            await session.execute(
                text("SELECT count(*) FROM run WHERE tenant_id = :d AND id > :baseline"),
                {"d": default_id, "baseline": baseline_default_run_id},
            )
        ).scalar_one()
    assert default_runs_for_this == 0, "scoped trigger must not run the default tenant"

    # cleanup this run
    async with factory() as session:
        await session.execute(text("DELETE FROM run WHERE id = :r"), {"r": run.id})
        await session.commit()


async def test_superadmin_trigger_still_fans_out(
    customer_and_admin: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, _admin_id, default_id = customer_and_admin
    _patch_heavy(monkeypatch)
    set_scheduler(SchedulerService(open_active_session, base_url="http://test.local"))

    factory = get_session_factory()
    superadmin = None
    try:
        async with factory() as session:
            superadmin = await user_repo.create(
                session,
                username=f"h1-super-{uuid.uuid4().hex[:8]}",
                password_hash="x",
                role="superadmin",
                is_sso=False,
            )
            request = _FakeRequest()  # superadmin path ignores the claim
            await trigger(
                request,
                TriggerRequest(dry_run=True),
                superadmin,
                session,  # type: ignore[arg-type]
            )

        async with factory() as session:
            for tid in (default_id, cid):
                cnt = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM run WHERE tenant_id = :t AND trigger='manual' "
                            "AND started_at >= now() - interval '1 minute'"
                        ),
                        {"t": tid},
                    )
                ).scalar_one()
                assert cnt >= 1, f"superadmin fan-out missed tenant {tid}"
    finally:
        async with factory() as session:
            await session.execute(
                text(
                    "DELETE FROM run WHERE trigger='manual' "
                    "AND started_at >= now() - interval '2 minutes'"
                )
            )
            if superadmin is not None:
                await session.execute(
                    text("DELETE FROM app_user WHERE id = :u"), {"u": superadmin.id}
                )
            await session.commit()
