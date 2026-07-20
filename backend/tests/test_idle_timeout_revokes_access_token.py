"""TDD for L2: the idle-timeout must invalidate already-issued access tokens.

`_end_if_idle` (app/api/routes/auth.py) deletes the refresh-session row and clears the
cookies, but `get_current_user` (app/api/deps.py) never consults the session row -- it
gates on the `gen` claim of the access token vs `AppUser.token_generation`. Without a
generation bump, an access token minted before the timeout stays valid until it naturally
expires (up to `access_token_ttl_min`, 15 min). A stolen token therefore survives the idle
logout. This test drives `/auth/activity` (which runs `_end_if_idle`) with a session pushed
past the idle window, then asserts the pre-timeout access token is rejected right away.

Uses the savepoint-isolated `session` fixture -- a single connection, so the generation
bump committed inside `_end_if_idle` is visible to the follow-up `get_current_user` call.

`activity` is `@limiter.limit`-decorated; slowapi rejects a duck-typed request unless the
limiter is disabled, so the autouse fixture below disables it (same pattern as
`test_access_token_revocation.py`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from app.api import routes
from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, get_current_user, limiter
from app.api.routes.auth import activity
from app.core.errors import AuthError
from app.core.security import hash_token, issue_token_pair
from app.models._base import utcnow
from app.repositories import user_repo
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _limiter_disabled() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    """Duck-typed Request -- `activity`/`get_current_user` only read `.cookies`
    (`activity` also reads `.headers`/`.client` via audit metadata on timeout)."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    """Duck-typed Response -- captures `delete_cookie` calls from `clear_auth_cookies`."""

    def __init__(self) -> None:
        self.deleted_cookies: set[str] = set()

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        pass

    def delete_cookie(self, name: str, **_: object) -> None:
        self.deleted_cookies.add(name)


async def test_idle_timeout_revokes_stale_access_token(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Small, explicit idle window so the seeded session lands past it deterministically.
    monkeypatch.setattr(routes.auth._settings, "idle_timeout_min", 1)

    user = await user_repo.create(
        session, username="idle-revoke@local", password_hash="x", role="admin"
    )
    assert user.id is not None
    user_id = user.id

    pair = issue_token_pair(str(user_id), generation=user.token_generation)
    us = await user_repo.create_session(
        session,
        user_id=user_id,
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=None,
        ip=None,
    )
    # Push last activity well past the (1-min) idle window so `_end_if_idle` fires.
    us.last_used_at = utcnow() - dt.timedelta(minutes=5)
    await session.commit()

    # Control: the access token is accepted BEFORE the idle timeout.
    access_request = _FakeRequest({ACCESS_COOKIE: pair.access_token})
    got = await get_current_user(access_request, session)  # type: ignore[arg-type]
    assert got.id == user_id

    # Drive the idle path via the activity ping -> `_end_if_idle` ends the session.
    activity_request = _FakeRequest({REFRESH_COOKIE: pair.refresh_token})
    activity_response = _FakeResponse()
    with pytest.raises(AuthError) as timeout_exc:
        await activity(activity_request, activity_response, session)  # type: ignore[arg-type]
    assert timeout_exc.value.code == "session_idle_timeout"

    # The access token issued before the timeout must now be dead -- the generation was
    # bumped, so its `gen` claim no longer matches `AppUser.token_generation`.
    with pytest.raises(AuthError) as revoked_exc:
        await get_current_user(access_request, session)  # type: ignore[arg-type]
    assert revoked_exc.value.code == "token_revoked"
