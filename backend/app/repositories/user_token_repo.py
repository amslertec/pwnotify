"""DB-Zugriff für Einmal-Tokens (Einladung, Passwort-Reset) -- `models/token.py`.

Instanzweite Tabelle wie `app_user`/`user_session` (kein RLS, siehe `db/rls.py::RLS_TABLES`).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models._base import utcnow
from ..models.token import UserToken


async def create(
    session: AsyncSession,
    *,
    app_user_id: int,
    purpose: str,
    token_hash: str,
    expires_at: dt.datetime,
    created_by: int,
) -> UserToken:
    token = UserToken(
        app_user_id=app_user_id,
        purpose=purpose,
        token_hash=token_hash,
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    return token


async def get_live_by_hash(
    session: AsyncSession, token_hash: str, purpose: str
) -> UserToken | None:
    """Nur ein NOCH gültiges Token: unverbraucht, nicht abgelaufen, passender `purpose`.

    Jeder andere Zustand (nie existiert, falscher Zweck, abgelaufen, bereits verbraucht)
    liefert bewusst dasselbe `None` -- die aufrufende Route darf daraus NIE unterschiedliche
    Fehlermeldungen ableiten (keine Enumeration, siehe `api/routes/public_tokens.py`)."""
    res = await session.execute(
        select(UserToken).where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == purpose,
            UserToken.consumed_at.is_(None),
            UserToken.expires_at > utcnow(),
        )
    )
    return res.scalar_one_or_none()


async def consume(session: AsyncSession, token: UserToken) -> None:
    """Markiert das Token als verbraucht (Single-Use) -- `consumed_at` gesetzt heisst
    endgültig unbrauchbar, auch innerhalb der eigenen Gültigkeitsspanne."""
    token.consumed_at = utcnow()
    await session.commit()


async def consume_live_for_user(session: AsyncSession, *, app_user_id: int, purpose: str) -> None:
    """Entwertet ALLE noch gültigen Tokens dieses Zwecks für das Konto -- idempotent.

    Ein neu ausgestellter Reset-Link soll der einzig gültige sein: sonst könnte ein älterer,
    per Mail noch offener Link (z. B. aus einem abgebrochenen ersten Versuch) parallel
    weiterverwendet werden (§7c: 'ein neues Token ersetzt ältere Reset-Tokens')."""
    res = await session.execute(
        select(UserToken).where(
            UserToken.app_user_id == app_user_id,
            UserToken.purpose == purpose,
            UserToken.consumed_at.is_(None),
            UserToken.expires_at > utcnow(),
        )
    )
    now = utcnow()
    for tok in res.scalars().all():
        tok.consumed_at = now
    await session.commit()


async def delete_created_by(session: AsyncSession, user_id: int) -> None:
    """Löscht Tokens, die dieses Konto ALS ADMIN ausgestellt hat (`created_by`) --
    Carry-forward-Fix aus Task 1: `created_by` trägt bewusst KEIN `ON DELETE` (ein
    gelöschtes Erstellerkonto darf ein noch gültiges Token eines ANDEREN Nutzers nicht
    mitreissen) -- ohne diesen Aufräumschritt VOR dem Löschen des Erstellerkontos selbst
    schlägt das Löschen mit einem `IntegrityError` fehl, sobald noch Tokens offen sind, die
    dieser Admin ausgestellt hat. Mirror von `user_repo.delete`s expliziter
    Sessions-Löschung (dieselbe Begründung: kein ORM-Relationship, DELETE erzwingt die
    Reihenfolge)."""
    await session.execute(sa_delete(UserToken).where(UserToken.created_by == user_id))
    await session.commit()
