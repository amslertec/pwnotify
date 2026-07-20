"""DB access for one-time tokens (invitation, password reset) -- `models/token.py`.

Instance-wide table like `app_user`/`user_session` (no RLS, see `db/rls.py::RLS_TABLES`).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
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
    """Only a token that is STILL valid: unconsumed, not expired, matching `purpose`.

    Every other state (never existed, wrong purpose, expired, already consumed)
    deliberately returns the same `None` -- the calling route must NEVER derive different
    error messages from that (no enumeration, see `api/routes/public_tokens.py`)."""
    res = await session.execute(
        select(UserToken).where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == purpose,
            UserToken.consumed_at.is_(None),
            UserToken.expires_at > utcnow(),
        )
    )
    return res.scalar_one_or_none()


async def consume(session: AsyncSession, token: UserToken) -> bool:
    """Marks the token as consumed (single-use) -- ATOMIC, guarded UPDATE (TOCTOU fix):
    `UPDATE ... WHERE id=:id AND consumed_at IS NULL RETURNING id` instead of the earlier
    ORM attribute set (`token.consumed_at = utcnow()`). Two parallel requests that BOTH
    read the same still-valid row via `get_live_by_hash` before either one committed must
    not both succeed in consuming it -- the `WHERE consumed_at IS NULL` guard only lets the
    FIRST commit through, every further UPDATE on the same (now already consumed) row
    returns 0 hits.

    Returns `True` if THIS call consumed the token (already committed).
    Returns `False` if it was already consumed in the meantime (by another, concurrent
    request) -- NO commit in that case. The caller MUST treat `False` identically to
    'token never found' (the same generic `token_invalid` error, no enumeration, see
    `api/routes/public_tokens.py`)."""
    res = await session.execute(
        sa_update(UserToken)
        .where(UserToken.id == token.id, UserToken.consumed_at.is_(None))
        .values(consumed_at=utcnow())
        .returning(UserToken.id)
    )
    if res.first() is None:
        return False
    await session.commit()
    return True


async def consume_live_for_user(session: AsyncSession, *, app_user_id: int, purpose: str) -> None:
    """Invalidates ALL still-valid tokens of this purpose for the account -- idempotent.

    A newly issued reset link is meant to be the only valid one: otherwise an older link
    still open via email (e.g. from an aborted first attempt) could be used in parallel
    (§7c: 'a new token replaces older reset tokens')."""
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
    """Deletes tokens that this account issued AS ADMIN (`created_by`) -- carry-forward
    fix from Task 1: `created_by` deliberately carries NO `ON DELETE` (a deleted issuer
    account must not drag down a still-valid token belonging to ANOTHER user) -- without
    this cleanup step BEFORE deleting the issuer account itself, the deletion fails with an
    `IntegrityError` as soon as tokens issued by this admin are still open. Mirrors
    `user_repo.delete`'s explicit session deletion (same rationale: no ORM relationship,
    DELETE enforces the ordering).

    Does NOT commit (M-03): this step sits in `delete_user` between the staged
    `USER_DELETED` audit entry and the actual account deletion. An internal commit here
    would prematurely finalize the audit entry staged before it, so that a later failure
    would leave a phantom deletion in the log. The sole caller (`admin_users.delete_user`)
    commits audit + deletion together at the end."""
    await session.execute(sa_delete(UserToken).where(UserToken.created_by == user_id))
