"""DB-Zugriff für Scheduler-Läufe."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.run import Run


async def create(session: AsyncSession, *, trigger: str, dry_run: bool) -> Run:
    run = Run(trigger=trigger, dry_run=dry_run, status="running")
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


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
