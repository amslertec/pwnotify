"""APScheduler-Anbindung: geplanter Lauf, manueller Trigger, Reschedule, Shutdown."""

from __future__ import annotations

import asyncio
import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.logging import get_logger
from ..models.run import Run
from .runner import execute_run
from .settings_service import SettingsService

log = get_logger("scheduler")

_JOB_ID = "pwnotify-run"


class SchedulerService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], base_url: str):
        self.session_factory = session_factory
        self.base_url = base_url
        self._scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._running = False

    # -- Lifecycle ----------------------------------------------------------- #
    async def start(self) -> None:
        cron, tz = await self._read_schedule()
        self._scheduler.start()
        self._add_job(cron, tz)
        log.info("scheduler_started", cron=cron, timezone=tz)

    async def shutdown(self) -> None:
        # wait=True -> laufender Job wird sauber zu Ende geführt (graceful).
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        log.info("scheduler_stopped")

    # -- Konfiguration ------------------------------------------------------- #
    async def _read_schedule(self) -> tuple[str, str]:
        async with self.session_factory() as session:
            svc = SettingsService(session)
            data = await svc.get_all()
        return (data.get("schedule.cron") or "0 8 * * *", data.get("schedule.timezone") or "UTC")

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
        cron, tz = await self._read_schedule()
        self._add_job(cron, tz)
        log.info("scheduler_rescheduled", cron=cron, timezone=tz)

    # -- Ausführung ---------------------------------------------------------- #
    async def _job(self) -> None:
        await self._run(trigger="schedule")

    async def trigger_now(self, dry_run_override: bool | None = None) -> Run:
        return await self._run(trigger="manual", dry_run_override=dry_run_override)

    async def _run(self, *, trigger: str, dry_run_override: bool | None = None) -> Run:
        async with self._lock:  # verhindert Überlappung manuell/geplant
            self._running = True
            try:
                return await execute_run(
                    self.session_factory,
                    trigger=trigger,
                    dry_run_override=dry_run_override,
                    base_url=self.base_url,
                )
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


# Modul-Singleton (in main.py gesetzt)
_service: SchedulerService | None = None


def set_scheduler(service: SchedulerService) -> None:
    global _service
    _service = service


def get_scheduler() -> SchedulerService:
    if _service is None:
        raise RuntimeError("Scheduler nicht initialisiert")
    return _service


def compute_next_runs(cron: str, tz: str, count: int = 5) -> list[dt.datetime]:
    """Standalone-Vorschau (für Settings-UI, ohne den laufenden Job zu berühren)."""
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
