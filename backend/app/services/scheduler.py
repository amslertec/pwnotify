"""APScheduler integration: scheduled run, manual trigger, reschedule, shutdown."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..db.tenant_context import tenant_scoped_session, use_owner_context, use_tenant
from ..models.run import Run
from ..repositories import tenant_repo
from .runner import execute_run
from .settings_service import SettingsService

log = get_logger("scheduler")

_JOB_ID = "pwnotify-run"


class SchedulerService:
    def __init__(self, session_factory: Callable[[], AsyncSession], base_url: str):
        self.session_factory = session_factory
        self.base_url = base_url
        self._scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._running = False

    # -- Lifecycle ----------------------------------------------------------- #
    async def start(self) -> None:
        cron, tz = await self._read_default_schedule()
        self._scheduler.start()
        self._add_job(cron, tz)
        log.info("scheduler_started", cron=cron, timezone=tz)

    async def shutdown(self) -> None:
        # wait=True -> the running job is allowed to finish cleanly (graceful).
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        log.info("scheduler_stopped")

    # -- Configuration --------------------------------------------------------- #
    async def _read_schedule(self, session: AsyncSession) -> tuple[str, str]:
        """Reads ``schedule.cron``/``schedule.timezone`` on the PASSED-IN session, which is
        already scoped appropriately. Previously (Phase-3 TODO) this ran on an unscoped
        owner session -- since RLS doesn't apply to the owner role, `select(Setting)` there
        produced an undefined mix of the `schedule.*` rows of ALL tenants once a second one
        existed (last row read wins, no filtering). Callers are now always unambiguously
        scoped: `_read_default_schedule` (the ONE global APScheduler job -- real staggered
        multi-tenant scheduling with per-customer job times remains Design §8, its own
        follow-up task, see `_active_tenant_ids`) and `_run` (each customer reads its OWN
        schedule within its own `use_tenant` block)."""
        svc = SettingsService(session)
        data = await svc.get_all()
        return (data.get("schedule.cron") or "0 8 * * *", data.get("schedule.timezone") or "UTC")

    async def _read_default_schedule(self) -> tuple[str, str]:
        """Drives the ONE global APScheduler job (`start`/`reschedule`) -- deterministically
        scoped to the default tenant instead of blindly across all tenants.

        `reschedule()` can run from a request route that is itself already under an active
        (non-default) tenant (`tenant_scoped_session`, see `api/routes/settings.py`).
        `self.session_factory` is `open_active_session` -- without an explicit
        `use_owner_context()` block, the lookup below would then run on the runtime engine
        (role `pwnotify_app`, GUC = the ACTIVE foreign tenant) instead of on the owner.
        `tenant` isn't RLS-scoped and `pwnotify_app` currently still has SELECT on it (see
        migration `f7a8b9c0d1e2`), so the result would be identical today -- but that's a
        hidden dependency on exactly that grant, not a deliberate decision. The owner context
        makes the lookup independent of it."""
        with use_owner_context():
            async with self.session_factory() as owner:
                tenant = await tenant_repo.default_tenant(owner)
        assert tenant.id is not None  # persisted row from the DB
        async with tenant_scoped_session(tenant.id) as session:
            return await self._read_schedule(session)

    def _add_job(self, cron: str, tz: str) -> None:
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
        self._scheduler.add_job(
            self._job,
            trigger=trigger,
            id=_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    async def reschedule(self) -> None:
        cron, tz = await self._read_default_schedule()
        self._add_job(cron, tz)
        log.info("scheduler_rescheduled", cron=cron, timezone=tz)

    # -- Execution ------------------------------------------------------------- #
    async def _job(self) -> None:
        await self._run(trigger="schedule")

    async def trigger_now(
        self, dry_run_override: bool | None = None, *, tenant_ids: list[int] | None = None
    ) -> Run:
        return await self._run(
            trigger="manual", dry_run_override=dry_run_override, tenant_ids=tenant_ids
        )

    async def _active_tenant_ids(self) -> list[int]:
        """Reads active customers on an owner session (no tenant context active).

        TRANSITION (Phase 3 -> Phase 4): today only the one default tenant exists, so the
        loop below effectively runs once -- identical to the previous behavior. Real
        multi-tenant runs (staggered start times, concurrency limit) are Design §8 and a
        separate follow-up task.
        """
        with use_owner_context():
            async with self.session_factory() as session:
                res = await session.execute(text("SELECT id FROM tenant WHERE is_active"))
                rows = res.scalars().all()
        return [int(tid) for tid in rows]

    async def _run(
        self,
        *,
        trigger: str,
        dry_run_override: bool | None = None,
        tenant_ids: list[int] | None = None,
    ) -> Run:
        async with self._lock:  # prevents overlap between manual and scheduled runs
            self._running = True
            try:
                # None -> all active tenants (scheduled fan-out); an explicit list -> exactly
                # those (a scoped manual trigger passes its single authorized tenant).
                ids = tenant_ids if tenant_ids is not None else await self._active_tenant_ids()
                if not ids:
                    raise RuntimeError(
                        "Kein aktiver Kunde vorhanden -- der Lauf hat keinen Tenant zum "
                        "Ausführen gefunden."
                    )
                last_run: Run | None = None
                errors = 0
                for tenant_id in ids:
                    # A3: isolate each tenant. A single broken/misconfigured customer must
                    # never abort the run for the tenants AFTER it in the list -- errors that
                    # escape execute_run's inner handlers (e.g. run_repo.create /
                    # session.commit raising outside them) would otherwise break the loop and
                    # silently skip every later customer's expiry notifications.
                    try:
                        # Each customer runs isolated under its own tenant context -- the
                        # session opened in the runner is thereby automatically
                        # tenant-scoped (RLS applies), see `apply_tenant_on_begin`.
                        async with use_tenant(tenant_id):
                            # Read this customer's own schedule (tenant-scoped, see
                            # `_read_schedule` docstring) -- currently only drives
                            # visibility (log), not yet a staggered per-customer run time (§8).
                            async with self.session_factory() as tsession:
                                cron, tz = await self._read_schedule(tsession)
                            log.info(
                                "run_tenant_schedule", tenant_id=tenant_id, cron=cron, timezone=tz
                            )
                            last_run = await execute_run(
                                self.session_factory,
                                trigger=trigger,
                                dry_run_override=dry_run_override,
                                base_url=self.base_url,
                            )
                    except Exception as exc:
                        errors += 1
                        log.error("tenant_run_failed", tenant_id=tenant_id, error=str(exc))
                        continue  # next customer -- one broken tenant must not abort the rest
                if last_run is None:
                    # Every tenant in the fan-out failed (each already logged above). There is
                    # no Run to return, so replace the old bare `assert last_run is not None`
                    # (which hid *why* and how many) with a loud, explicit error.
                    log.error("all_tenant_runs_failed", count=len(ids), errors=errors)
                    raise RuntimeError(
                        f"Alle {len(ids)} Kunden-Läufe sind fehlgeschlagen -- kein Lauf erstellt."
                    )
                return last_run
            finally:
                self._running = False

    # -- Status -------------------------------------------------------------- #
    @property
    def is_running(self) -> bool:
        return self._running

    def next_run_times(self, count: int = 5) -> list[dt.datetime]:
        job = self._scheduler.get_job(_JOB_ID)
        if not job:
            return []
        times: list[dt.datetime] = []
        prev: dt.datetime | None = None
        now = dt.datetime.now(job.trigger.timezone)
        for _ in range(count):
            nxt = job.trigger.get_next_fire_time(prev, now if prev is None else prev)
            if not nxt:
                break
            times.append(nxt)
            prev = nxt
        return times

    def next_run_time(self) -> dt.datetime | None:
        times = self.next_run_times(1)
        return times[0] if times else None


# Module-level singleton (set in main.py)
_service: SchedulerService | None = None


def set_scheduler(service: SchedulerService) -> None:
    global _service
    _service = service


def get_scheduler() -> SchedulerService:
    if _service is None:
        raise RuntimeError("Scheduler nicht initialisiert")
    return _service


def compute_next_runs(cron: str, tz: str, count: int = 5) -> list[dt.datetime]:
    """Standalone preview (for the settings UI, without touching the running job)."""
    trigger = CronTrigger.from_crontab(cron, timezone=tz)
    times: list[dt.datetime] = []
    prev: dt.datetime | None = None
    now = dt.datetime.now(trigger.timezone)
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, now if prev is None else prev)
        if not nxt:
            break
        times.append(nxt)
        prev = nxt
    return times
