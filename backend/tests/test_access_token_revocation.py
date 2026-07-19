"""TDD for Task 2 (L1): access tokens become revocable via a per-user
`token_generation` (see `app/models/user.py::AppUser.token_generation`).

`get_current_user` already loads the `AppUser` row on every request -- comparing the
`gen` claim of the access token against `user.token_generation` is therefore a field
access, zero extra DB roundtrips. Bumping the generation (`user_repo.bump_token_generation`)
invalidates every access token issued before the bump; `logout`, `revoke_all`
(reuse detection in `refresh`), and `change_password` all bump it.

Uses the savepoint-isolated `session` fixture -- all three tests operate on a single
connection, no cross-connection visibility issue.

`change_password` is `@limiter.limit`-decorated (L7); slowapi rejects a duck-typed request
unless the limiter is disabled, so the autouse fixture below disables it for these direct
calls (same pattern as `test_invitation_flow.py`).
"""

from __future__ import annotations

from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, get_current_user, limiter
from app.api.routes.auth import change_password, logout, revoke_other_sessions
from app.core.errors import AuthError
from app.core.security import decode_token, hash_password, hash_token, issue_token_pair
from app.repositories import user_repo
from app.schemas.auth import PasswordChangeRequest
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
    """Duck-typed Request -- the routes/dependency under test only read `.cookies`
    (`logout`/`change_password` also read `.headers`/`.client` for audit metadata)."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    """Duck-typed Response -- captures `set_cookie`/`delete_cookie` calls made by
    `set_auth_cookies`/`clear_auth_cookies` without needing a real ASGI response."""

    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}
        self.deleted_cookies: set[str] = set()

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:
        self.deleted_cookies.add(name)


def _extract_cookie(raw_header: str, name: str) -> str | None:
    jar: SimpleCookie = SimpleCookie()
    jar.load(raw_header)
    return jar[name].value if name in jar else None


async def _make_user(session: AsyncSession, *, username: str, password: str | None = None) -> int:
    pw_hash = hash_password(password) if password else "x"
    user = await user_repo.create(session, username=username, password_hash=pw_hash, role="admin")
    assert user.id is not None
    return user.id


async def _seed_session(session: AsyncSession, *, user_id: int, access_gen: int) -> tuple[str, str]:
    """Issues a token pair carrying `access_gen` in the `gen` claim and persists a matching
    `user_session` row for the refresh token. Returns (access_token, refresh_token)."""
    pair = issue_token_pair(str(user_id), generation=access_gen)
    await user_repo.create_session(
        session,
        user_id=user_id,
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=None,
        ip=None,
    )
    return pair.access_token, pair.refresh_token


# ---- Core: bumping the generation revokes the old access token, a fresh one works --- #


async def test_stale_access_token_rejected_after_generation_bump(session: AsyncSession) -> None:
    user_id = await _make_user(session, username="revoke-core@local")
    user = await user_repo.get(session, user_id)
    assert user is not None

    pair = issue_token_pair(str(user_id), generation=user.token_generation)
    old_request = _FakeRequest({ACCESS_COOKIE: pair.access_token})

    # GREEN control: the freshly issued token is accepted before any bump.
    got = await get_current_user(old_request, session)  # type: ignore[arg-type]
    assert got.id == user_id

    await user_repo.bump_token_generation(session, user_id)
    await session.commit()

    with pytest.raises(AuthError) as exc_info:
        await get_current_user(old_request, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "token_revoked"

    # A token minted with the NEW generation must be accepted.
    await session.refresh(user)
    new_pair = issue_token_pair(str(user_id), generation=user.token_generation)
    new_request = _FakeRequest({ACCESS_COOKIE: new_pair.access_token})
    got_again = await get_current_user(new_request, session)  # type: ignore[arg-type]
    assert got_again.id == user_id


# ---- logout bumps the generation -> the access token held before logout is dead ----- #


async def test_logout_revokes_access_token(session: AsyncSession) -> None:
    user_id = await _make_user(session, username="revoke-logout@local")
    user = await user_repo.get(session, user_id)
    assert user is not None
    access_token, refresh_token = await _seed_session(session, user_id=user_id, access_gen=0)

    # Control: the access token is valid before logout.
    access_request = _FakeRequest({ACCESS_COOKIE: access_token})
    got = await get_current_user(access_request, session)  # type: ignore[arg-type]
    assert got.id == user_id

    logout_request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    logout_response = _FakeResponse()
    await logout(logout_request, logout_response, session)  # type: ignore[arg-type]

    with pytest.raises(AuthError) as exc_info:
        await get_current_user(access_request, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "token_revoked"


# ---- change_password bumps the generation, kills the caller's OLD access token, but --- #
# ---- re-issues fresh cookies so the caller stays logged in on THIS device ------------ #


async def test_password_change_revokes_old_access_token_but_keeps_caller(
    session: AsyncSession,
) -> None:
    old_password = "OldPassw0rd!"
    new_password = "NewPassw0rd!"
    user_id = await _make_user(session, username="revoke-pwchange@local", password=old_password)
    user = await user_repo.get(session, user_id)
    assert user is not None
    old_access_token, refresh_token = await _seed_session(session, user_id=user_id, access_gen=0)

    # Control: the pre-change access token is valid.
    old_access_request = _FakeRequest({ACCESS_COOKIE: old_access_token})
    got = await get_current_user(old_access_request, session)  # type: ignore[arg-type]
    assert got.id == user_id

    change_request = _FakeRequest({REFRESH_COOKIE: refresh_token})
    change_response = _FakeResponse()
    body = PasswordChangeRequest(current_password=old_password, new_password=new_password)
    await change_password(
        request=change_request,  # type: ignore[arg-type]
        body=body,
        user=user,
        session=session,
        response=change_response,  # type: ignore[arg-type]
    )

    # (a) the OLD access token is now revoked.
    with pytest.raises(AuthError) as exc_info:
        await get_current_user(old_access_request, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "token_revoked"

    # (b) fresh auth cookies were issued on the response.
    assert ACCESS_COOKIE in change_response.cookie_values
    assert REFRESH_COOKIE in change_response.cookie_values

    # (c) the NEW access token is accepted.
    new_access_request = _FakeRequest({ACCESS_COOKIE: change_response.cookie_values[ACCESS_COOKIE]})
    got_again = await get_current_user(new_access_request, session)  # type: ignore[arg-type]
    assert got_again.id == user_id


# ---- revoke_other_sessions bumps the generation -> the OTHER device's access token --- #
# ---- dies IMMEDIATELY (not just after its refresh session is revoked), while the ----- #
# ---- caller's own device is re-issued fresh cookies and stays logged in -------------- #


async def test_revoke_others_revokes_other_devices_access_token_immediately(
    session: AsyncSession,
) -> None:
    user_id = await _make_user(session, username="revoke-others@local")
    user = await user_repo.get(session, user_id)
    assert user is not None

    # Two devices, both with a live access token at generation 0.
    current_access_token, current_refresh_token = await _seed_session(
        session, user_id=user_id, access_gen=0
    )
    other_access_token, _other_refresh_token = await _seed_session(
        session, user_id=user_id, access_gen=0
    )

    # Control: both access tokens are valid before the call.
    current_access_request = _FakeRequest({ACCESS_COOKIE: current_access_token})
    other_access_request = _FakeRequest({ACCESS_COOKIE: other_access_token})
    assert (await get_current_user(current_access_request, session)).id == user_id  # type: ignore[arg-type]
    assert (await get_current_user(other_access_request, session)).id == user_id  # type: ignore[arg-type]

    revoke_request = _FakeRequest({REFRESH_COOKIE: current_refresh_token})
    revoke_response = _FakeResponse()
    await revoke_other_sessions(
        request=revoke_request,  # type: ignore[arg-type]
        user=user,
        session=session,
        response=revoke_response,  # type: ignore[arg-type]
    )

    # (a) the OTHER device's access token is dead RIGHT NOW -- not in up to 15 minutes.
    with pytest.raises(AuthError) as exc_info:
        await get_current_user(other_access_request, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "token_revoked"

    # (b) fresh auth cookies were issued for the caller's own device.
    assert ACCESS_COOKIE in revoke_response.cookie_values
    assert REFRESH_COOKIE in revoke_response.cookie_values

    # (c) the NEW access token for the caller's own device is accepted.
    new_current_access_request = _FakeRequest(
        {ACCESS_COOKIE: revoke_response.cookie_values[ACCESS_COOKIE]}
    )
    got_again = await get_current_user(new_current_access_request, session)  # type: ignore[arg-type]
    assert got_again.id == user_id

    # (d) exactly one active refresh session remains -- the caller's own, rotated to the
    # new refresh token issued on the response. The other device's session is revoked.
    rows = await user_repo.list_sessions(session, user_id)
    assert len(rows) == 1
    new_refresh_jti = decode_token(
        revoke_response.cookie_values[REFRESH_COOKIE], expected_type="refresh"
    )["jti"]
    assert rows[0].refresh_jti == new_refresh_jti
