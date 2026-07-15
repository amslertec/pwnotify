"""DB-Zugriff für Scheduler-Läufe."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models._base import utcnow
from ..models.run import Run


async def create(session: AsyncSession, *, trigger: str, dry_run: bool) -> Run:
    run = Run(trigger=trigger, dry_run=dry_run, status="running")
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def mark_stale_as_error(session: AsyncSession) -> int:
    """Beim Start hängengebliebene Läufe abschliessen. Gibt deren Anzahl zurück.

    Ein Lauf wird als ``running`` angelegt und erst am Ende abgeschlossen. Stirbt der
    Prozess dazwischen (Neustart, Deploy, OOM), bleibt der Eintrag für immer auf
    ``running`` — die Historie zeigt dann einen Lauf, der nie endet, und die Statistik
    stimmt nicht mehr. Da pro Prozess nur ein Lauf gleichzeitig möglich ist
    (``max_instances=1`` + Lock), kann beim Start kein echter Lauf mehr offen sein.
    """
    rows = (await session.execute(select(Run).where(Run.status == "running"))).scalars().all()
    now = utcnow()
    for run in rows:
        run.status = "error"
        run.error = "Der Lauf wurde durch einen Neustart der Anwendung unterbrochen."
        run.finished_at = now
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    if rows:
        await session.commit()
    return len(rows)


async def get(session: AsyncSession, run_id: int) -> Run | None:
    return await session.get(Run, run_id)


async def latest(session: AsyncSession) -> Run | None:
    res = await session.execute(select(Run).order_by(Run.started_at.desc()).limit(1))
    return res.scalar_one_or_none()


async def list_runs(
    session: AsyncSession, *, page: int = 1, page_size: int = 25
) -> tuple[list[Run], int]:
    total = int((await session.execute(select(func.count(Run.id)))).scalar_one())
    res = await session.execute(
        select(Run).order_by(Run.started_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return list(res.scalars().all()), total
