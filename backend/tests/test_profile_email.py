"""TDD für Task 5 (Console+Groups+Invite), §7d: `POST /auth/profile` (`update_profile`)
pflegt jetzt zusätzlich `email` -- NUR für lokale Konten. Ein SSO-Konto bezieht seine
Adresse aus Entra; ein hier eingetragener Wert wird ignoriert, statt sie stillschweigend zu
überschreiben.

Treibt Route-Funktionen direkt an (Muster wie die übrige Suite), savepoint-isolierte
`session`-Fixture aus `conftest.py`."""

from __future__ import annotations

import uuid

from app.api.routes.auth import me, update_profile
from app.models.user import AppUser
from app.schemas.auth import ProfileUpdate
from sqlalchemy.ext.asyncio import AsyncSession


async def _mk_user(session: AsyncSession, *, is_sso: bool, email: str | None = None) -> AppUser:
    u = AppUser(
        username=f"pe5-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=is_sso,
        email=email,
    )
    session.add(u)
    await session.flush()
    return u


async def test_local_account_sets_email_persisted_and_reflected_in_me(
    session: AsyncSession,
) -> None:
    user = await _mk_user(session, is_sso=False)
    new_email = f"new-{uuid.uuid4().hex[:8]}@pe5.test"

    out = await update_profile(  # type: ignore[arg-type]
        ProfileUpdate(display_name="Neuer Name", email=new_email), user, session, None
    )
    assert out.email == new_email
    assert out.display_name == "Neuer Name"

    refreshed = await session.get(AppUser, user.id)
    assert refreshed is not None and refreshed.email == new_email

    me_out = await me(user, session, None)  # type: ignore[arg-type]
    assert me_out.email == new_email


async def test_local_account_can_clear_email(session: AsyncSession) -> None:
    user = await _mk_user(session, is_sso=False, email="old@pe5.test")

    out = await update_profile(  # type: ignore[arg-type]
        ProfileUpdate(display_name=None, email=None), user, session, None
    )
    assert out.email is None
    refreshed = await session.get(AppUser, user.id)
    assert refreshed is not None and refreshed.email is None


async def test_sso_account_email_edit_is_ignored(session: AsyncSession) -> None:
    user = await _mk_user(session, is_sso=True, email=None)
    attacker_supplied = "attacker@pe5.test"

    out = await update_profile(  # type: ignore[arg-type]
        ProfileUpdate(display_name="SSO Name", email=attacker_supplied), user, session, None
    )
    assert out.email is None
    assert out.display_name == "SSO Name"  # display_name bleibt weiterhin editierbar

    refreshed = await session.get(AppUser, user.id)
    assert refreshed is not None and refreshed.email is None

    me_out = await me(user, session, None)  # type: ignore[arg-type]
    assert me_out.email is None
