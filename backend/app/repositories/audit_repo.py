"""DB-Zugriff auf das Audit-Protokoll (Anlegen, Blättern, Aufräumen)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditLog
from ..services.retention import purge_blocked_reason


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
    """Delete entries older than ``days``. 0 = keep forever.

    A safety brake mirrors the privacy-retention guard: if the purge would remove more than
    half of all audit rows it is almost certainly a misconfiguration (e.g. a tiny retention
    window wiping the trail) — nothing is deleted.
    """
    if days <= 0:
        return 0
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    to_delete = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.at < cutoff)
        )
    ).scalar_one()
    if not to_delete:
        return 0
    total = (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    if purge_blocked_reason(to_delete=int(to_delete), total=int(total)) is not None:
        return 0
    await session.execute(sa_delete(AuditLog).where(AuditLog.at < cutoff))
    await session.commit()
    return int(to_delete)
