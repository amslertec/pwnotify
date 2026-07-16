"""DB-Zugriff für lokale UI-Accounts und Refresh-Sessions."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import AppUser, UserSession


# ---- AppUser ---------------------------------------------------------------- #
async def get_by_username(session: AsyncSession, username: str) -> AppUser | None:
    res = await session.execute(select(AppUser).where(AppUser.username == username))
    return res.scalar_one_or_none()


async def get(session: AsyncSession, user_id: int) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(AppUser.id)))).scalar_one())


async def count_admins(session: AsyncSession) -> int:
    """Aktive Administratoren (lokal + SSO). Basis für den Schutz vor Aussperrung."""
    stmt = select(func.count(AppUser.id)).where(
        AppUser.role == "admin", AppUser.is_active.is_(True)
    )
    return int((await session.execute(stmt)).scalar_one())


async def create(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    role: str = "admin",
    display_name: str | None = None,
    is_sso: bool = False,
) -> AppUser:
    user = AppUser(
        username=username,
        password_hash=password_hash,
        role=role,
        display_name=display_name,
        is_sso=is_sso,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def list_all(session: AsyncSession) -> list[AppUser]:
    res = await session.execute(select(AppUser).order_by(AppUser.username))
    return list(res.scalars().all())


async def list_sso(session: AsyncSession) -> list[AppUser]:
    res = await session.execute(select(AppUser).where(AppUser.is_sso.is_(True)))
    return list(res.scalars().all())


async def delete(session: AsyncSession, user_id: int) -> None:
    user = await session.get(AppUser, user_id)
    if user is not None:
        # ALLE Sessions zuerst und explizit entfernen. Zwei Fallstricke:
        # 1. `list_sessions` blendet widerrufene/abgelaufene Sessions aus, deren
        #    Fremdschlüssel aber weiter auf den Benutzer zeigt — nach einem Logout
        #    scheiterte das Löschen so an user_session_user_id_fkey.
        # 2. Zwischen AppUser und UserSession ist keine Relationship definiert, der
        #    ORM kennt die Abhängigkeit also nicht und würde die Reihenfolge frei
        #    wählen. Ein direktes DELETE läuft sofort und damit garantiert zuerst.
        await session.execute(sa_delete(UserSession).where(UserSession.user_id == user_id))
        await session.delete(user)
        await session.commit()


# ---- Sessions (Refresh-Token-Rotation) ------------------------------------- #
async def create_session(
    session: AsyncSession,
    *,
    user_id: int,
    jti: str,
    token_hash: str,
    expires_at: dt.datetime,
    user_agent: str | None,
    ip: str | None,
) -> UserSession:
    us = UserSession(
        user_id=user_id,
        refresh_jti=jti,
        token_hash=token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip,
    )
    session.add(us)
    await session.commit()
    await session.refresh(us)
    return us


async def get_session_by_jti(session: AsyncSession, jti: str) -> UserSession | None:
    res = await session.execute(select(UserSession).where(UserSession.refresh_jti == jti))
    return res.scalar_one_or_none()


async def list_sessions(session: AsyncSession, user_id: int) -> list[UserSession]:
    now = dt.datetime.now(dt.UTC)
    res = await session.execute(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked.is_(False),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_used_at.desc())
    )
    return list(res.scalars().all())


async def revoke_others(session: AsyncSession, user_id: int, keep_jti: str | None) -> int:
    """Alle aktiven Sitzungen des Users ausser der aktuellen abmelden."""
    count = 0
    for us in await list_sessions(session, user_id):
        if us.refresh_jti != keep_jti:
            us.revoked = True
            count += 1
    await session.commit()
    return count


async def prune_sessions(session: AsyncSession, user_id: int) -> None:
    """Abgelaufene/abgemeldete Sitzungs-Datensätze des Users entfernen (Aufräumen)."""
    now = dt.datetime.now(dt.UTC)
    res = await session.execute(
        select(UserSession).where(
            UserSession.user_id == user_id,
            or_(UserSession.revoked.is_(True), UserSession.expires_at <= now),
        )
    )
    for us in res.scalars().all():
        await session.delete(us)
    await session.commit()


async def delete_session_by_jti(session: AsyncSession, jti: str) -> None:
    """Sitzung vollständig entfernen (Abmeldung, Inaktivität) statt nur zu widerrufen."""
    await session.execute(sa_delete(UserSession).where(UserSession.refresh_jti == jti))
    await session.commit()


async def revoke_session(session: AsyncSession, jti: str) -> None:
    us = await get_session_by_jti(session, jti)
    if us:
        us.revoked = True
        await session.commit()


async def revoke_all(session: AsyncSession, user_id: int) -> None:
    for us in await list_sessions(session, user_id):
        us.revoked = True
    await session.commit()
