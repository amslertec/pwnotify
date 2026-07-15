"""DB-Zugriff auf das Audit-Protokoll (Anlegen, Blättern, Aufräumen)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditLog


def build(
    *,
    actor_id: int | None,
    actor_username: str | None,
    actor_type: str,
    action: str,
    target: str | None,
    outcome: str,
    ip_address: str | None,
    user_agent: str | None,
    detail: dict[str, Any],
) -> AuditLog:
    return AuditLog(
        actor_id=actor_id,
        actor_username=actor_username,
        actor_type=actor_type,
        action=action,
        target=target,
        outcome=outcome,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail,
    )


async def list_paged(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    action: str | None = None,
    actor: str | None = None,
    outcome: str | None = None,
    since: dt.datetime | None = None,
) -> tuple[list[AuditLog], int]:
    """Seite des Protokolls + Gesamtzahl (neueste zuerst)."""
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if actor:
        stmt = stmt.where(AuditLog.actor_username == actor)
        count_stmt = count_stmt.where(AuditLog.actor_username == actor)
    if outcome:
        stmt = stmt.where(AuditLog.outcome == outcome)
        count_stmt = count_stmt.where(AuditLog.outcome == outcome)
    if since:
        stmt = stmt.where(AuditLog.at >= since)
        count_stmt = count_stmt.where(AuditLog.at >= since)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AuditLog.at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def distinct_actions(session: AsyncSession) -> list[str]:
    """Vorhandene Aktionsarten — speist den Filter in der Oberfläche."""
    res = await session.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))
    return list(res.scalars().all())


async def purge_older_than(session: AsyncSession, *, days: int) -> int:
    """Einträge älter als ``days`` entfernen. 0 = unbegrenzt aufbewahren."""
    if days <= 0:
        return 0
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    count = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.at < cutoff)
        )
    ).scalar_one()
    if count:
        await session.execute(sa_delete(AuditLog).where(AuditLog.at < cutoff))
        await session.commit()
    return int(count)
