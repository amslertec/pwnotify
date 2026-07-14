"""DB-Zugriff für Ausschluss-Regeln."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.entra import Exclusion


async def list_all(session: AsyncSession) -> list[Exclusion]:
    res = await session.execute(select(Exclusion).order_by(Exclusion.created_at.desc()))
    return list(res.scalars().all())


async def add(session: AsyncSession, *, kind: str, value: str, label: str | None) -> Exclusion:
    exc = Exclusion(kind=kind, value=value, label=label)
    session.add(exc)
    await session.commit()
    await session.refresh(exc)
    return exc


async def delete(session: AsyncSession, exclusion_id: int) -> None:
    exc = await session.get(Exclusion, exclusion_id)
    if exc:
        await session.delete(exc)
        await session.commit()


async def group_ids(session: AsyncSession) -> list[str]:
    res = await session.execute(select(Exclusion.value).where(Exclusion.kind == "group"))
    return list(res.scalars().all())


async def user_values(session: AsyncSession) -> list[str]:
    res = await session.execute(select(Exclusion.value).where(Exclusion.kind == "user"))
    return list(res.scalars().all())
