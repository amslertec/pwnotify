"""APScheduler-Anbindung: geplanter Lauf, manueller Trigger, Reschedule, Shutdown."""

from __future__ import annotations

import asyncio
import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.logging import get_logger
from ..db.tenant_context import tenant_scoped_session, use_tenant
from ..models.run import Run
from ..repositories import tenant_repo
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
        cron, tz = await self._read_default_schedule()
        self._scheduler.start()
        self._add_job(cron, tz)
        log.info("scheduler_started", cron=cron, timezone=tz)

    async def shutdown(self) -> None:
        # wait=True -> laufender Job wird sauber zu Ende geführt (graceful).
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        log.info("scheduler_stopped")

    # -- Konfiguration ------------------------------------------------------- #
    async def _read_schedule(self, session: AsyncSession) -> tuple[str, str]:
        """Liest ``schedule.cron``/``schedule.timezone`` auf der ÜBERGEBENEN, bereits
        passend gescopten Session. Vormals (Phase-3-TODO) lief das auf einer unscoped
        Owner-Session -- weil RLS für die Owner-Rolle nicht greift, ergab `select(Setting)`
        dort ein undefiniertes Gemisch aus den `schedule.*`-Zeilen ALLER Tenants, sobald ein
        zweiter existierte (letzte gelesene Zeile gewinnt, keine Filterung). Aufrufer sind
        jetzt immer eindeutig gescoped: `_read_default_schedule` (der EINE globale
        APScheduler-Job -- echtes gestaffeltes Multi-Tenant-Scheduling mit eigenen
        Job-Zeiten pro Kunde bleibt Design §8, ein eigener Folge-Task, siehe
        `_active_tenant_ids`) und `_run` (jeder Kunde liest sein EIGENES Schedule innerhalb
        seines eigenen `use_tenant`-Blocks)."""
        svc = SettingsService(session)
        data = await svc.get_all()
        return (data.get("schedule.cron") or "0 8 * * *", data.get("schedule.timezone") or "UTC")

    async def _read_default_schedule(self) -> tuple[str, str]:
        """Treibt den EINEN globalen APScheduler-Job (`start`/`reschedule`) -- deterministisch
        über den Default-Tenant gescoped statt blind über alle Tenants hinweg."""
        async with self.session_factory() as owner:
            tenant = await tenant_repo.default_tenant(owner)
        assert tenant.id is not None  # persistierte Zeile aus der DB
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

    # -- Ausführung ---------------------------------------------------------- #
    async def _job(self) -> None:
        await self._run(trigger="schedule")

    async def trigger_now(
        self, dry_run_override: bool | None = None, *, tenant_ids: list[int] | None = None
    ) -> Run:
        return await self._run(
            trigger="manual", dry_run_override=dry_run_override, tenant_ids=tenant_ids
        )

    async def _active_tenant_ids(self) -> list[int]:
        """Aktive Kunden auf einer Owner-Session lesen (kein Tenant-Kontext aktiv).

        ÜBERGANG (Phase 3 -> Phase 4): heute existiert nur der eine Default-Tenant, die
        Schleife unten läuft also faktisch einmal -- identisch zum bisherigen Verhalten.
        Echte Mehrmandanten-Läufe (gestaffelte Startzeiten, Concurrency-Limit) sind
        Design §8 und ein eigener Folge-Task.
        """
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
        async with self._lock:  # verhindert Überlappung manuell/geplant
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
                for tenant_id in ids:
                    # Jeder Kunde läuft isoliert unter seinem eigenen Tenant-Kontext --
                    # die im Runner geöffnete Session wird dadurch automatisch
                    # tenant-gescopt (RLS greift), siehe `apply_tenant_on_begin`.
                    async with use_tenant(tenant_id):
                        # Eigenes Schedule dieses Kunden lesen (tenant-gescopt, s.
                        # `_read_schedule`-Docstring) -- treibt aktuell nur Sichtbarkeit
                        # (Log), noch keine gestaffelte Ausführungszeit pro Kunde (§8).
                        async with self.session_factory() as tsession:
                            cron, tz = await self._read_schedule(tsession)
                        log.info("run_tenant_schedule", tenant_id=tenant_id, cron=cron, timezone=tz)
                        last_run = await execute_run(
                            self.session_factory,
                            trigger=trigger,
                            dry_run_override=dry_run_override,
                            base_url=self.base_url,
                        )
                assert last_run is not None  # tenant_ids ist nicht leer (s. o.)
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
