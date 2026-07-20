"""Tests for Task 4: per-tenant background run (scheduler + runner).

Before this task EVERY run (cron job as well as manual trigger) failed: `execute_run`
opened a plain owner session (no active tenant context), but `run_repo.create` creates
`Run(tenant_id=current_tenant_or_none())` -- without a context this resolves to `None`,
and `run.tenant_id` is NOT NULL -> the INSERT failed. Now `SchedulerService` reads the
active customers on an owner session and runs `execute_run` per customer inside
`use_tenant(tenant_id)` -- the session opened in the runner thereby becomes automatically
tenant-scoped (begin listener), so `run_repo.create` stamps the correct active tenant.

`oidc.sync_sso_users` writes `app_user` (instance-wide, no RLS) and must therefore run on
a separate OWNER session, even while the enclosing run is tenant-scoped -- this is checked
directly here (`current_user`/GUC in the session the runner passes to the (mocked)
`sync_sso_users` call).

Graph/mail calls are too heavy for a real end-to-end run (real network) --
`sync_users`, `oidc.sync_sso_users`, `_resolve_excluded_ids` and the admin alert are
therefore mocked. The goal of this suite is the wiring (tenant loop, owner session
for the SSO sync, trigger path no longer raises), not the domain sync logic itself
(that is tested elsewhere).

Seed/cleanup pattern as in `test_route_tenant_scoping.py`: real superuser connection on
`migrated_engine`, really committed, cleanup in `finally` (the savepoint-isolated
`session` fixture doesn't fit here -- the begin listener needs a real BEGIN).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from app.db.tenant_context import open_active_session
from app.services.scheduler import SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    """Independent control query (not via a module cache)."""
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


@pytest_asyncio.fixture
async def second_tenant_with_schedule(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[tuple[int, str, str]]:
    """A second ACTIVE tenant with its OWN, exotic `schedule.cron`/
    `schedule.timezone` -- proof for Task 5's fix: `_read_schedule` must no longer read
    blindly across all tenants (Phase-3 TODO: an unscoped owner session saw, because RLS
    doesn't apply to the owner role, an undefined mix of ALL `schedule.*` rows once a
    second tenant exists)."""
    cron, tz = "*/13 * * * *", "Pacific/Kiritimati"
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sts5Second','sts5-second',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
                "(:tid, 'schedule.cron', to_jsonb(CAST(:cron AS text)), false, now()), "
                "(:tid, 'schedule.timezone', to_jsonb(CAST(:tz AS text)), false, now())"
            ),
            {"tid": tid, "cron": cron, "tz": tz},
        )
        await conn.commit()
        try:
            yield tid, cron, tz
        finally:
            await conn.execute(text("DELETE FROM run WHERE tenant_id = :tid"), {"tid": tid})
            await conn.execute(text("DELETE FROM setting WHERE tenant_id = :tid"), {"tid": tid})
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


@pytest_asyncio.fixture
async def inactive_tenant(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """A second, INACTIVE tenant -- the run loop must not touch it."""
    async with migrated_engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sts4Inactive','sts4-inactive',false,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield tid
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tid})
            await conn.commit()


def _patch_heavy_dependencies(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def _fake_sync_users(session: AsyncSession, settings: dict[str, Any]) -> dict[str, int]:
        return {"checked": 0}

    async def _fake_sso_sync(
        session: AsyncSession, settings: dict[str, Any], *, tenant_id: int
    ) -> dict[str, int]:
        # The actual proof for point 2 of the task: this function must be called by the
        # runner with an OWNER session -- despite an active use_tenant(...) wrapped
        # around it. `app_user` is instance-wide, no role switch/GUC allowed here.
        captured["oidc_role"] = (await session.execute(text("SELECT current_user"))).scalar_one()
        captured["oidc_guc"] = (
            await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        return {"synced": 0, "removed": 0}

    async def _no_excluded(session: Any, settings: dict[str, Any]) -> set[str]:
        return set()

    async def _no_alert(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.services.runner.sync_users", _fake_sync_users)
    monkeypatch.setattr("app.services.oidc.sync_sso_users", _fake_sso_sync)
    monkeypatch.setattr("app.services.runner._resolve_excluded_ids", _no_excluded)
    monkeypatch.setattr("app.services.alerts.maybe_send_run_alert", _no_alert)


async def test_trigger_now_creates_run_stamped_with_active_tenant_id(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core proof of the bugfix: `trigger_now` no longer raises (NOT-NULL violation) and
    the created run carries the `tenant_id` of the active (default) tenant."""
    dtid = await _real_default_tenant_id(migrated_engine)
    captured: dict[str, Any] = {}
    _patch_heavy_dependencies(monkeypatch, captured)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid, f"Lauf nicht dem aktiven Tenant zugeordnet: {run.tenant_id}"
        assert captured["oidc_role"] == "pwnotify", (
            f"sync_sso_users lief nicht als Owner-Rolle: {captured['oidc_role']}"
        )
        # NULL (never set) OR '' (reset value of a custom GUC on a reused pool
        # connection, see the same convention in test_runtime_isolation.py) both
        # count as "no tenant" -- exactly the app's own fail-safe convention
        # (`NULLIF(current_setting(...), '')` in the RLS policy, migration `c4d5e6f7a8b9`).
        assert not captured["oidc_guc"], (
            "Owner-Session darf kein Tenant-GUC gesetzt haben (SET LOCAL ist "
            f"transaktionsgebunden): {captured['oidc_guc']!r}"
        )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


async def test_trigger_now_does_not_touch_inactive_tenant(
    migrated_engine: AsyncEngine, inactive_tenant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tenant loop only reads ACTIVE customers -- an inactive tenant gets no
    run of its own, even though it exists in the `tenant` table."""
    dtid = await _real_default_tenant_id(migrated_engine)
    captured: dict[str, Any] = {}
    _patch_heavy_dependencies(monkeypatch, captured)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid
        async with migrated_engine.connect() as conn:
            foreign_runs = (
                await conn.execute(
                    text("SELECT count(*) FROM run WHERE tenant_id = :tid"),
                    {"tid": inactive_tenant},
                )
            ).scalar_one()
        assert foreign_runs == 0, "Ein inaktiver Tenant hat trotzdem einen Lauf bekommen"
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


async def test_run_reads_each_tenants_own_schedule(
    migrated_engine: AsyncEngine,
    second_tenant_with_schedule: tuple[int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 5: closes the Phase-3 TODO. `_read_schedule` is now called inside the
    tenant loop (tenant-scoped session) -- each customer gets back its OWN
    `schedule.cron`/`schedule.timezone`, not a mix of all of them."""
    dtid = await _real_default_tenant_id(migrated_engine)
    second_tid, second_cron, second_tz = second_tenant_with_schedule
    captured: dict[str, Any] = {}
    _patch_heavy_dependencies(monkeypatch, captured)

    reads: list[tuple[int | None, str, str]] = []
    orig_read_schedule = SchedulerService._read_schedule

    async def _spy_read_schedule(self: SchedulerService, session: Any) -> tuple[str, str]:
        from app.db.tenant_context import current_tenant_or_none

        cron, tz = await orig_read_schedule(self, session)
        reads.append((current_tenant_or_none(), cron, tz))
        return cron, tz

    monkeypatch.setattr(SchedulerService, "_read_schedule", _spy_read_schedule)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        by_tenant = {tid: (cron, tz) for tid, cron, tz in reads}
        assert by_tenant.get(second_tid) == (second_cron, second_tz), (
            f"Zweiter Tenant bekam nicht sein eigenes Schedule: {by_tenant.get(second_tid)}"
        )
        assert dtid in by_tenant, "Default-Tenant wurde in der Schleife nicht gelesen"
        assert by_tenant[dtid] != by_tenant[second_tid], (
            f"Beide Tenants lieferten dasselbe Schedule -- Blend-Bug nicht behoben: {by_tenant}"
        )

        # `_read_default_schedule` (drives the ONE global APScheduler job) is
        # deterministically scoped to the default tenant, not to the second tenant.
        default_cron, default_tz = await service._read_default_schedule()
        assert (default_cron, default_tz) != (second_cron, second_tz)
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


async def test_fanout_isolates_a_failing_tenant(
    migrated_engine: AsyncEngine,
    second_tenant_with_schedule: tuple[int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3: a single broken/misconfigured tenant must NEVER abort the run for the tenants
    after it in the fan-out. With two active tenants the FIRST one's `execute_run` raises;
    the SECOND one must still be executed and produce its own run.

    Against the old code (no per-tenant try/except) the first exception propagates straight
    out of the loop -> `trigger_now` raises and the second tenant is silently skipped.
    """
    second_tid, _, _ = second_tenant_with_schedule
    assert second_tid  # two active tenants exist (default + this one)

    from app.db.tenant_context import current_tenant_or_none

    seen: list[int | None] = []

    async def _fake_execute_run(
        session_factory: Any, *, trigger: str, dry_run_override: bool | None, base_url: str
    ) -> Any:
        tid = current_tenant_or_none()
        seen.append(tid)
        # The FIRST tenant in the fan-out blows up (e.g. a DB error outside execute_run's
        # inner handlers); every later tenant must still be processed.
        if len(seen) == 1:
            raise RuntimeError("first tenant blew up")
        return SimpleNamespace(id=None, tenant_id=tid)

    monkeypatch.setattr("app.services.scheduler.execute_run", _fake_execute_run)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    assert len(seen) == 2, f"Fan-out brach nach dem ersten Fehler ab: {seen}"
    assert seen[0] != seen[1], f"Beide Iterationen liefen unter demselben Tenant: {seen}"
    assert run is not None and run.tenant_id == seen[1], (
        "Der zurückgegebene Lauf gehört nicht zum zweiten (erfolgreichen) Tenant"
    )
