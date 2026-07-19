"""Task 3 (H4 full closure): background tenant writes must run through `pwnotify_runtime`.

Before this task, `SchedulerService`/`execute_run`/`seed.run_seed` opened their tenant-scoped
writes on `get_session_factory()` -- the OWNER engine (superuser login `pwnotify`). Inside
`use_tenant(tid)` the begin-listener still does `SET LOCAL ROLE pwnotify_app`, so the write was
RLS-checked at the SQL level, but the underlying login role never left the superuser -- any code
path that (accidentally or via a bug) skipped `SET LOCAL ROLE` reached the tables as superuser,
bypassing RLS entirely. `open_active_session()` (`app/db/tenant_context.py`) closes that gap: it
re-reads the `current_tenant_id` ContextVar at CALL time and returns a session on the
non-superuser `pwnotify_runtime` engine whenever a tenant is active, and on the owner engine
otherwise -- so background tenant writes can no longer reach Postgres as superuser at all.

Three groups of tests:
1. The opener itself: routes to runtime under an active tenant, to owner without one (the
   crux -- distinguishes `session_user`, the login role unmasked by `SET ROLE`, from
   `current_user`, the SET-ROLE'd role).
2. A real `SchedulerService` run (constructed with the opener, exactly like production
   `main.py`) for two active tenants: runs on `pwnotify_runtime` and stays RLS-isolated between
   the two tenants -- the actual production path, not just the opener in isolation.
3. Owner-context counter-checks: the two owner-only cross-tenant paths (`mark_stale_as_error`,
   `get_audit_session` for a superadmin) and a NULL-tenant `audit.record` write are unaffected
   -- they must keep bypassing RLS via the owner engine, not get routed to runtime by mistake.

Seed-/cleanup pattern like `test_runtime_isolation.py`: real committed superuser connections on
`migrated_engine`, cleanup in `finally` -- the begin-listener needs a real `BEGIN`, so the
savepoint-isolated `session` fixture does not fit here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.api.deps import get_audit_session
from app.db.session import get_session_factory
from app.db.tenant_context import open_active_session, tenant_scoped_session, use_tenant
from app.repositories import run_repo, tenant_repo
from app.services import audit
from app.services.audit import LOGIN_FAILED
from app.services.scheduler import SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Independent control query (not via a module-level cache) -- same helper pattern as
    `test_runtime_isolation.py`/`test_scheduler_tenant_scope.py`."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


@pytest_asyncio.fixture
async def two_active_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[tuple[int, int]]:
    """Two REAL active tenants (not default-vs-foreign) -- same pattern as
    `test_runtime_isolation.py::two_active_tenants`, trimmed to just the tenant rows since
    `execute_run` creates its own `run` row per tenant."""
    async with migrated_engine.connect() as conn:
        a, b = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Bri A','bri-a',true,now()), ('Bri B','bri-b',true,now()) "
                        "RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        try:
            yield int(a), int(b)
        finally:
            await conn.execute(
                text("DELETE FROM run WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
            )
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


# ---- 1. The opener itself: routes by the active-tenant ContextVar (the crux) -------------- #


async def test_active_session_uses_runtime_role_under_tenant(migrated_engine: AsyncEngine) -> None:
    """Inside `use_tenant(...)`, `open_active_session()` must open on the `pwnotify_runtime`
    LOGIN role (`session_user`), with `SET LOCAL ROLE pwnotify_app` applied (`current_user`) --
    the same as `tenant_scoped_session`, but decided dynamically per call instead of a fixed
    engine baked into a context manager."""
    dtid = await _real_default_tenant_id(migrated_engine)
    async with use_tenant(dtid), open_active_session() as s:
        su, cu = (await s.execute(text("SELECT session_user, current_user"))).one()
    assert su == "pwnotify_runtime", f"tenant write not on runtime engine: {su}"
    assert cu == "pwnotify_app", f"SET LOCAL ROLE not applied: {cu}"


async def test_active_session_uses_owner_role_without_tenant() -> None:
    """Without an active tenant (owner/instance-wide context), `open_active_session()` must
    stay on the owner engine -- no login-role change, no SET ROLE, RLS bypassed by table
    ownership exactly as before."""
    async with open_active_session() as s:
        su, cu = (await s.execute(text("SELECT session_user, current_user"))).one()
    assert su == "pwnotify" and cu == "pwnotify", f"owner context not on owner engine: {su}/{cu}"


# ---- 2. A real scheduler run for two active tenants: runtime + stays RLS-isolated --------- #


def _patch_heavy_run_dependencies(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, list[tuple[str, str]]]
) -> None:
    """Same network-/mail-heavy stubs as `test_runtime_isolation.py::_patch_heavy_run_dependencies`
    -- the target here is the tenant loop's engine routing, not the sync itself. `sync_users`
    additionally records `session_user`/`current_user` of the tenant-scoped run session (the
    one `execute_run` opens via `session_factory()` at runner.py's top, inside `use_tenant`)."""

    async def _fake_sync_users(session: Any, settings: dict[str, Any]) -> dict[str, int]:
        row = (await session.execute(text("SELECT session_user, current_user"))).one()
        captured.setdefault("session_user_pairs", []).append((row[0], row[1]))
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


async def test_scheduler_run_uses_runtime_role_and_stays_isolated(
    migrated_engine: AsyncEngine,
    two_active_tenants: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructs `SchedulerService` the way `main.py` does in production -- with
    `open_active_session`, not a fixed `session_factory` -- and drives a real
    `trigger_now(tenant_ids=[a, b])`. Proves two things at once: (a) the run's tenant-scoped
    session lands on `pwnotify_runtime`/`pwnotify_app` for BOTH tenants, and (b) each tenant's
    run stays invisible to the other under RLS, exactly like before the change -- the runtime
    routing must not weaken isolation."""
    a, b = two_active_tenants
    captured: dict[str, list[tuple[str, str]]] = {}
    _patch_heavy_run_dependencies(monkeypatch, captured)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    before = dt.datetime.now(dt.UTC)
    await service.trigger_now(dry_run_override=True, tenant_ids=[a, b])

    pairs = captured.get("session_user_pairs", [])
    assert len(pairs) == 2, f"expected one captured run session per tenant, got {pairs}"
    for su, cu in pairs:
        assert su == "pwnotify_runtime", f"scheduler run not on runtime engine: {su}"
        assert cu == "pwnotify_app", f"SET LOCAL ROLE not applied in scheduler run: {cu}"

    async with migrated_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, tenant_id FROM run WHERE started_at >= :ts"), {"ts": before}
            )
        ).all()
    try:
        tenant_ids = [int(r.tenant_id) for r in rows]
        for expected in (a, b):
            assert tenant_ids.count(expected) == 1, (
                f"expected exactly one run for tenant {expected}, saw {tenant_ids}"
            )
        run_by_tenant = {int(r.tenant_id): int(r.id) for r in rows}

        async with tenant_scoped_session(a) as s:
            ids = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        assert run_by_tenant[a] in ids and run_by_tenant[b] not in ids, f"leak toward A: {ids}"

        async with tenant_scoped_session(b) as s:
            ids = set((await s.execute(text("SELECT id FROM run"))).scalars().all())
        assert run_by_tenant[b] in ids and run_by_tenant[a] not in ids, f"leak toward B: {ids}"
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM run WHERE id = ANY(:ids)"), {"ids": [int(r.id) for r in rows]}
            )
            await conn.commit()


async def test_read_default_schedule_looks_up_tenant_on_owner_role_under_foreign_tenant(
    migrated_engine: AsyncEngine,
    two_active_tenants: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_read_default_schedule`'s tenant lookup (`tenant_repo.default_tenant`) must run on the
    OWNER `session_user`, even when called from inside an already-active NON-default tenant
    context -- e.g. `reschedule()` triggered from the tenant settings route under
    `tenant_scoped_session` (see `api/routes/settings.py`, `contextlib.suppress(RuntimeError)`
    around `get_scheduler().reschedule()`).

    Callsite review finding (Task 3 follow-up): `self.session_factory` is `open_active_session`,
    which re-reads the active-tenant ContextVar at call time. Without an explicit
    `use_owner_context()` around the lookup, it would route onto the `pwnotify_runtime` engine
    (role `pwnotify_app`, GUC = whichever FOREIGN tenant happens to be active) instead of the
    owner engine. `tenant` happens to be exempt from RLS and `pwnotify_app` still has SELECT on
    it (migration `f7a8b9c0d1e2`), so the returned row is correct either way today -- but that
    correctness is a hidden dependency on those grants, not a deliberate scope. This test proves
    the lookup itself runs on the owner role, independent of any such grant."""
    a, _b = two_active_tenants
    captured: dict[str, tuple[str, str]] = {}
    real_default_tenant = tenant_repo.default_tenant

    async def _capturing_default_tenant(session: Any) -> Any:
        row = (await session.execute(text("SELECT session_user, current_user"))).one()
        captured["pair"] = (row[0], row[1])
        return await real_default_tenant(session)

    monkeypatch.setattr(
        "app.services.scheduler.tenant_repo.default_tenant", _capturing_default_tenant
    )

    service = SchedulerService(open_active_session, base_url="http://test.local")
    async with use_tenant(a):
        cron, tz = await service._read_default_schedule()

    assert "pair" in captured, "tenant_repo.default_tenant was not called"
    su, cu = captured["pair"]
    assert (su, cu) == ("pwnotify", "pwnotify"), (
        f"tenant lookup leaked onto the active foreign tenant's role/GUC: {su}/{cu}"
    )
    # Sanity: still resolves the real default tenant's schedule, not tenant `a`'s.
    dtid = await _real_default_tenant_id(migrated_engine)
    assert dtid != a
    assert isinstance(cron, str) and isinstance(tz, str)


# ---- 3. Owner-context counter-checks: the audit trail / cross-tenant paths stay owner ----- #


async def test_mark_stale_as_error_stays_cross_tenant_on_owner_factory(
    migrated_engine: AsyncEngine, two_active_tenants: tuple[int, int]
) -> None:
    """Startup housekeeping (`main.py`) keeps `run_repo.mark_stale_as_error` on the explicit
    owner factory -- it must still see and update stuck `running` rows across BOTH tenants in
    one call, exactly like before this task (this path was never touched)."""
    a, b = two_active_tenants
    async with migrated_engine.connect() as conn:
        run_a, run_b = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO run "
                        "(tenant_id, trigger, dry_run, status, started_at, "
                        "checked_users, sent, failed, skipped, detail_log) VALUES "
                        "(:a,'manual',false,'running',now(),0,0,0,0,'[]'::jsonb), "
                        "(:b,'manual',false,'running',now(),0,0,0,0,'[]'::jsonb) "
                        "RETURNING id"
                    ),
                    {"a": a, "b": b},
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()

    async with get_session_factory()() as owner:
        stale = await run_repo.mark_stale_as_error(owner)

    assert stale >= 2, f"owner factory did not see the stuck runs across tenants: {stale}"
    async with migrated_engine.connect() as conn:
        statuses = (
            (
                await conn.execute(
                    text("SELECT status FROM run WHERE id IN (:ra, :rb)"),
                    {"ra": run_a, "rb": run_b},
                )
            )
            .scalars()
            .all()
        )
    assert list(statuses) == ["error", "error"], f"stuck runs not closed: {statuses}"


async def test_superadmin_audit_session_stays_cross_tenant_on_owner(
    migrated_engine: AsyncEngine, two_active_tenants: tuple[int, int]
) -> None:
    """`get_audit_session` (deps.py) keeps the superadmin branch on the owner session -- it
    must still read `audit_log` rows across BOTH tenants, unaffected by the runtime routing
    introduced in this task."""
    a, b = two_active_tenants

    class _FakeSuperadmin:
        is_sso = False
        role = "superadmin"

    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, actor_type, action, outcome, detail, at) "
                "VALUES "
                "(:a, 'system', 'test.marker', 'success', '{}'::jsonb, now()), "
                "(:b, 'system', 'test.marker', 'success', '{}'::jsonb, now())"
            ),
            {"a": a, "b": b},
        )
        await conn.commit()

    async with get_session_factory()() as owner:
        gen = get_audit_session(None, _FakeSuperadmin(), owner)  # type: ignore[arg-type]
        try:
            session = await anext(gen)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT tenant_id FROM audit_log "
                            "WHERE tenant_id IN (:a, :b) AND action = 'test.marker'"
                        ),
                        {"a": a, "b": b},
                    )
                )
                .scalars()
                .all()
            )
        finally:
            await gen.aclose()

    assert set(rows) == {a, b}, f"superadmin audit session did not see both tenants: {rows}"


async def test_owner_context_null_tenant_audit_record_commits_without_rls_reject() -> None:
    """An owner-context `audit.record(..., tenant_id=None via ContextVar)` -- the pattern used
    by admin routes like `admin_tenants.create_tenant` -- must still commit and be readable: no
    active tenant means the opener stays on the owner engine (superuser), so the RLS
    `WITH CHECK` (which would reject a NULL `tenant_id` for the non-owner `pwnotify_app` role)
    never applies here."""
    async with get_session_factory()() as owner:
        await audit.record(owner, action=LOGIN_FAILED, actor_username="rti-owner-null-tenant")
        await owner.commit()
        row = (
            await owner.execute(
                text(
                    "SELECT tenant_id FROM audit_log WHERE actor_username = 'rti-owner-null-tenant'"
                )
            )
        ).one()
        await owner.execute(
            text("DELETE FROM audit_log WHERE actor_username = 'rti-owner-null-tenant'")
        )
        await owner.commit()
    assert row.tenant_id is None, f"expected a NULL-tenant audit row, got {row.tenant_id}"
