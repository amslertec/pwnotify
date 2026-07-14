"""DB-Zugriff für das Versand-Protokoll."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.notification import NotificationLog


async def sent_stages(session: AsyncSession, entra_user_id: int, cycle: str) -> set[int]:
    """Bereits erfolgreich gesendete Reminder-Stufen für diesen Ablaufzyklus."""
    res = await session.execute(
        select(NotificationLog.reminder_day).where(
            NotificationLog.entra_user_id == entra_user_id,
            NotificationLog.expiry_cycle == cycle,
            NotificationLog.status == "sent",
        )
    )
    return set(res.scalars().all())


async def record(session: AsyncSession, data: dict[str, Any]) -> NotificationLog:
    """Upsert eines Log-Eintrags (bei Retry wird der bestehende Eintrag aktualisiert)."""
    stmt = (
        pg_insert(NotificationLog)
        .values(**data)
        .on_conflict_do_update(
            constraint="uq_notif_dedup",
            set_={
                "status": data["status"],
                "error": data.get("error"),
                "recipient": data["recipient"],
                "channel": data["channel"],
                "backend": data["backend"],
                "run_id": data.get("run_id"),
                "created_at": data["created_at"],
            },
        )
        .returning(NotificationLog)
    )
    res = await session.execute(stmt)
    return res.scalar_one()


async def get(session: AsyncSession, log_id: int) -> NotificationLog | None:
    return await session.get(NotificationLog, log_id)


async def list_logs(
    session: AsyncSession,
    *,
    status: str | None = None,
    entra_user_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[NotificationLog], int]:
    stmt = select(NotificationLog)
    count_stmt = select(func.count(NotificationLog.id))
    if status:
        stmt = stmt.where(NotificationLog.status == status)
        count_stmt = count_stmt.where(NotificationLog.status == status)
    if entra_user_id:
        stmt = stmt.where(NotificationLog.entra_user_id == entra_user_id)
        count_stmt = count_stmt.where(NotificationLog.entra_user_id == entra_user_id)
    total = int((await session.execute(count_stmt)).scalar_one())
    stmt = (
        stmt.order_by(NotificationLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def count_sent_since(session: AsyncSession, since: Any) -> int:
    res = await session.execute(
        select(func.count(NotificationLog.id)).where(
            NotificationLog.status == "sent", NotificationLog.created_at >= since
        )
    )
    return int(res.scalar_one())
