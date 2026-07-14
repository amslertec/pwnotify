"""DB-Zugriff für gespiegelte Entra-Benutzer."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Insert-or-update anhand ``entra_id`` (setzt beim Update ``excluded`` nicht zurück)."""
    stmt = pg_insert(EntraUser).values(**data)
    update_cols = {k: stmt.excluded[k] for k in data if k not in ("entra_id", "id", "excluded")}
    stmt = stmt.on_conflict_do_update(index_elements=["entra_id"], set_=update_cols)
    await session.execute(stmt)


async def get(session: AsyncSession, user_id: int) -> EntraUser | None:
    return await session.get(EntraUser, user_id)


async def get_by_entra_id(session: AsyncSession, entra_id: str) -> EntraUser | None:
    res = await session.execute(select(EntraUser).where(EntraUser.entra_id == entra_id))
    return res.scalar_one_or_none()


def _apply_filters(stmt: Select[Any], *, search: str | None, status: str | None) -> Select[Any]:
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
    # Shared Mailboxes nur in der eigenen Ansicht (status='shared'), sonst ausgeblendet.
    if status == "shared":
        stmt = stmt.where(EntraUser.is_shared.is_(True))
    else:
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
) -> tuple[list[EntraUser], int]:
    base = _apply_filters(select(EntraUser), search=search, status=status)

    total = (
        await session.execute(
            _apply_filters(select(func.count(EntraUser.id)), search=search, status=status)
        )
    ).scalar_one()

    col = SORTABLE.get(sort_by, EntraUser.days_left)
    order = col.asc().nulls_last() if sort_dir == "asc" else col.desc().nulls_last()
    stmt = base.order_by(order).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def iter_active_for_notification(session: AsyncSession) -> list[EntraUser]:
    """Kandidaten für Benachrichtigung: aktiv, nicht ausgeschlossen, hat Ablaufdatum."""
    stmt = select(EntraUser).where(
        EntraUser.account_enabled.is_(True),
        EntraUser.excluded.is_(False),
        EntraUser.is_shared.is_(False),
        EntraUser.expiry_date.is_not(None),
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_excluded(session: AsyncSession, user_id: int, excluded: bool) -> None:
    user = await session.get(EntraUser, user_id)
    if user:
        user.excluded = excluded
        await session.commit()


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
        # Shared Mailboxes zählen nie in den regulären KPIs mit.
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
    # Kategorien schliessen sich gegenseitig aus: deaktivierte Konten zählen nur unter
    # "disabled", die Ablauf-Kategorien (soon/expired/never/ok) nur aktive Konten.
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
    """Anzahl Ablauf-Ereignisse je Tag — nur aktive, nicht-shared, nicht-ausgeschlossene."""
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
