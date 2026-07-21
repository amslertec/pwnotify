"""DB access for mirrored Entra users."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.tenant_context import current_tenant_or_none
from ..models.entra import EntraUser

SORTABLE = {
    "display_name": EntraUser.display_name,
    "upn": EntraUser.upn,
    "mail": EntraUser.mail,
    "last_password_change": EntraUser.last_password_change,
    "expiry_date": EntraUser.expiry_date,
    "days_left": EntraUser.days_left,
    "account_enabled": EntraUser.account_enabled,
    "last_synced_at": EntraUser.last_synced_at,
}


async def upsert(session: AsyncSession, data: dict[str, Any]) -> None:
    """Insert-or-update keyed on ``entra_id`` (does not reset ``excluded`` on update).

    ``tenant_id`` comes explicitly from the active tenant context: this is a core
    ``pg_insert``, which (unlike ``session.add(EntraUser(...))``) does NOT go through the
    ORM ``default_factory`` -- without this value the NOT-NULL column would fail.
    """
    stmt = pg_insert(EntraUser).values(**data, tenant_id=current_tenant_or_none())
    update_cols = {k: stmt.excluded[k] for k in data if k not in ("entra_id", "id", "excluded")}
    stmt = stmt.on_conflict_do_update(index_elements=["tenant_id", "entra_id"], set_=update_cols)
    await session.execute(stmt)


async def get(session: AsyncSession, user_id: int) -> EntraUser | None:
    return await session.get(EntraUser, user_id)


async def get_by_entra_id(session: AsyncSession, entra_id: str) -> EntraUser | None:
    res = await session.execute(select(EntraUser).where(EntraUser.entra_id == entra_id))
    return res.scalar_one_or_none()


def _apply_filters(
    stmt: Select[Any], *, search: str | None, status: str | None, include_shared: bool = False
) -> Select[Any]:
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(EntraUser.display_name).like(like),
                func.lower(EntraUser.upn).like(like),
                func.lower(func.coalesce(EntraUser.mail, "")).like(like),
            )
        )
    if status == "expired":
        stmt = stmt.where(EntraUser.days_left <= 0, EntraUser.days_left.is_not(None))
    elif status == "soon":
        stmt = stmt.where(EntraUser.days_left > 0, EntraUser.days_left <= 7)
    elif status == "ok":
        stmt = stmt.where(EntraUser.days_left > 7)
    elif status == "never":
        stmt = stmt.where(EntraUser.days_left.is_(None))
    elif status == "disabled":
        stmt = stmt.where(EntraUser.account_enabled.is_(False))
    elif status == "excluded":
        stmt = stmt.where(EntraUser.excluded.is_(True))
    # Shared mailboxes normally only show up in their own view (status='shared') and are
    # hidden everywhere else. `include_shared` (sync test mode) lifts that hiding so the
    # default list surfaces shared/unlicensed accounts too -- consistent with test mode
    # already notifying them (see `iter_active_for_notification`).
    if status == "shared":
        stmt = stmt.where(EntraUser.is_shared.is_(True))
    elif not include_shared:
        stmt = stmt.where(EntraUser.is_shared.is_(False))
    return stmt


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
    sort_by: str = "days_left",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 25,
    include_shared: bool = False,
) -> tuple[list[EntraUser], int]:
    base = _apply_filters(
        select(EntraUser), search=search, status=status, include_shared=include_shared
    )

    total = (
        await session.execute(
            _apply_filters(
                select(func.count(EntraUser.id)),
                search=search,
                status=status,
                include_shared=include_shared,
            )
        )
    ).scalar_one()

    col = SORTABLE.get(sort_by, EntraUser.days_left)
    order = col.asc().nulls_last() if sort_dir == "asc" else col.desc().nulls_last()
    stmt = base.order_by(order).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def iter_active_for_notification(
    session: AsyncSession, *, include_inactive: bool = False
) -> list[EntraUser]:
    """Notification candidates: has an expiry date and is not excluded.

    Normally only enabled, licensed accounts qualify. ``include_inactive`` (sync test mode)
    drops the ``account_enabled``/``is_shared`` gates so disabled + unlicensed accounts become
    candidates too -- for exercising the real send/expiry flow. ``excluded`` and
    ``expiry_date`` always stay: an opted-out account or one without an expiry date is never
    notified, test mode or not.
    """
    conditions = [
        EntraUser.excluded.is_(False),
        EntraUser.expiry_date.is_not(None),
    ]
    if not include_inactive:
        conditions.append(EntraUser.account_enabled.is_(True))
        conditions.append(EntraUser.is_shared.is_(False))
    stmt = select(EntraUser).where(*conditions)
    return list((await session.execute(stmt)).scalars().all())


async def set_excluded(session: AsyncSession, user_id: int, excluded: bool) -> None:
    """Flip the exclusion flag. Does NOT commit -- callers (Security Phase 5, Task 8/M10)
    write an `audit_log` entry alongside this change and commit both atomically."""
    user = await session.get(EntraUser, user_id)
    if user:
        user.excluded = excluded


async def mark_group_excluded(session: AsyncSession, entra_ids: set[str], excluded: bool) -> int:
    if not entra_ids:
        return 0
    res = await session.execute(select(EntraUser).where(EntraUser.entra_id.in_(entra_ids)))
    count = 0
    for user in res.scalars().all():
        user.excluded = excluded
        count += 1
    return count


async def counts_for_dashboard(session: AsyncSession) -> dict[str, int]:
    async def _count(*conds: Any) -> int:
        # Shared mailboxes never count towards the regular KPIs.
        stmt = select(func.count(EntraUser.id)).where(EntraUser.is_shared.is_(False))
        for c in conds:
            stmt = stmt.where(c)
        return int((await session.execute(stmt)).scalar_one())

    shared = int(
        (
            await session.execute(
                select(func.count(EntraUser.id)).where(EntraUser.is_shared.is_(True))
            )
        ).scalar_one()
    )
    # Categories are mutually exclusive: disabled accounts only count under
    # "disabled", the expiry categories (soon/expired/never/ok) only count active accounts.
    active = EntraUser.account_enabled.is_(True)
    return {
        "total": await _count(),
        "expiring_soon": await _count(active, EntraUser.days_left > 0, EntraUser.days_left <= 7),
        "expired": await _count(active, EntraUser.days_left <= 0, EntraUser.days_left.is_not(None)),
        "never": await _count(active, EntraUser.days_left.is_(None)),
        "disabled": await _count(EntraUser.account_enabled.is_(False)),
        "shared": shared,
    }


async def expiry_histogram(session: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    """Number of expiry events per day -- active, non-shared, non-excluded only."""
    today = dt.datetime.now(dt.UTC).date()
    rows = (
        (
            await session.execute(
                select(EntraUser.expiry_date).where(
                    EntraUser.expiry_date.is_not(None),
                    EntraUser.account_enabled.is_(True),
                    EntraUser.is_shared.is_(False),
                    EntraUser.excluded.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    buckets: dict[str, int] = {}
    for exp in rows:
        d = exp.date()
        delta = (d - today).days
        if 0 <= delta <= days:
            buckets[d.isoformat()] = buckets.get(d.isoformat(), 0) + 1
    return [
        {
            "date": (today + dt.timedelta(days=i)).isoformat(),
            "count": buckets.get((today + dt.timedelta(days=i)).isoformat(), 0),
        }
        for i in range(days + 1)
    ]


async def count_all(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(EntraUser))).scalar_one())


async def count_stale(session: AsyncSession, *, days: int) -> int:
    """How many accounts have not been synced for ``days`` days?"""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    stmt = select(func.count()).select_from(EntraUser).where(EntraUser.last_synced_at < cutoff)
    return int((await session.execute(stmt)).scalar_one())


async def delete_stale(session: AsyncSession, *, days: int) -> int:
    """Remove accounts that have not shown up in a sync for ``days`` days.

    The caller must check :func:`services.retention.purge_blocked_reason` beforehand --
    otherwise a failed sync would make the entire dataset look stale.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    n = await count_stale(session, days=days)
    if n:
        await session.execute(sa_delete(EntraUser).where(EntraUser.last_synced_at < cutoff))
        await session.commit()
    return n
