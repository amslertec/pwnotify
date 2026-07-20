"""DB-Zugriff auf das Audit-Protokoll (Anlegen, Blättern, Aufräumen)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditLog
from ..services.retention import AUDIT_RETENTION_FLOOR_DAYS, purge_blocked_reason


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
    tenant_id: int | None = None,
    stamp_tenant: bool = False,
) -> AuditLog:
    """Build (but do not add/commit) an `AuditLog` row.

    Tenant attribution (Security Phase 5, Task 7/M11): `AuditLog.tenant_id` normally comes
    from its ORM `default_factory` (`current_tenant_or_none`), which stamps the active
    tenant on tenant-scoped sessions. Owner-session callers (no active tenant `ContextVar`)
    can still explicitly attribute an entry to one customer via `tenant_id` + `stamp_tenant`.

    `stamp_tenant` (rather than a `tenant_id is not None` check) is the deliberate signal
    that the caller passed a real override -- it separates "explicitly attribute this entry
    (possibly to `None`)" from "no override given, let the ORM default run". A plain
    `int | None` default of `None` could not tell those two cases apart without a sentinel;
    this keeps `build` fully mypy-clean with no `Any`/`cast` typing tricks. When
    `stamp_tenant` is `False`, `tenant_id` is ignored and the column's `default_factory`
    decides (as before this task).
    """
    if stamp_tenant:
        return AuditLog(
            tenant_id=tenant_id,
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

    Two protections:

    * A safety brake mirrors the privacy-retention guard: if the purge would remove more than
      half of all audit rows it is almost certainly a misconfiguration (e.g. a tiny retention
      window wiping the trail) — nothing is deleted.
    * A non-erasable floor (``AUDIT_RETENTION_FLOOR_DAYS``): any positive window shorter than
      the floor is treated AS the floor, so the most recent floor-days of history can never be
      purged. This is the defensive second layer behind the ``audit.retention_days`` validator
      (which already rejects a sub-floor window): even if a value ever bypassed the validator,
      the recent trail — including the SETTINGS_CHANGED entries that would document an admin
      shrinking the window — still survives. See ``retention.AUDIT_RETENTION_FLOOR_DAYS`` for
      why a hard floor is used instead of a stateful cumulative-window brake (YAGNI).

    Does NOT commit: the purge runs inside the caller's transaction (``runner.execute_run``),
    which commits once at the end of the run. Committing here would prematurely persist the
    caller's in-flight work (a foreign commit).
    """
    if days <= 0:
        return 0
    # Clamp a sub-floor window up to the floor -- never delete anything younger than the floor.
    days = max(days, AUDIT_RETENTION_FLOOR_DAYS)
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
    return int(to_delete)
