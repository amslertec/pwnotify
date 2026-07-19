"""TDD for Task 3 (M7): `login` (`app/api/routes/auth.py`) leaks account existence and
lock status through response *content*, independent of any wall-clock timing.

Two oracles existed before the fix:

1. **Verify short-circuit:** `if user is None or not verify_password(...)` never calls
   `verify_password` for an unknown username -- an attacker who can observe *whether*
   Argon2 ran (e.g. via a side channel, or simply because it is the only asymmetry between
   the "unknown user" and "wrong password" cases) can enumerate usernames. Proven here
   deterministically via a `monkeypatch` spy on `auth.verify_password` instead of a
   wall-clock timing assertion (flaky, forbidden by the plan) -- the spy proves the
   Argon2 call happens (or doesn't) regardless of how fast the machine running the suite
   is.
2. **Lock-before-password oracle:** a locked account rejected a WRONG password with
   `account_locked` *before* the password was even checked, revealing the lock (and thus
   the account's existence) to anyone who doesn't have the password.

Uses the `_limiter_disabled` + `_FakeRequest`/`_FakeResponse` pattern established by
`tests/test_2fa_enable_replay.py` / `tests/test_access_token_revocation.py`: `login` is
called directly (route function, not over HTTP), so the `@limiter.limit` decorator
disabled here to avoid needing a real ASGI request.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from app.api.routes.auth import login
from app.core import security as security_module
from app.core.errors import AuthError
from app.core.security import hash_password
from app.models._base import utcnow
from app.repositories import user_repo
from app.schemas.auth import LoginRequest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _limiter_disabled() -> Iterator[None]:
    from app.api.deps import limiter

    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    """Duck-typed Request -- `login` only reads `.client`/`.headers` for audit metadata."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:  # pragma: no cover
        pass


async def _make_user(session: AsyncSession, *, password: str) -> str:
    username = f"enum-{uuid.uuid4().hex}@local"
    await user_repo.create(
        session, username=username, password_hash=hash_password(password), role="admin"
    )
    return username


# ---- (1) verify_password must run even for an unknown username ------------------------ #


async def test_unknown_username_still_calls_verify_password(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_verify = security_module.verify_password

    def _spy(password: str, hashed: str) -> bool:
        nonlocal calls
        calls += 1
        return real_verify(password, hashed)

    monkeypatch.setattr("app.api.routes.auth.verify_password", _spy)

    body = LoginRequest(username=f"nobody-{uuid.uuid4().hex}@local", password="whatever-Wr0ng!")
    with pytest.raises(AuthError) as exc_info:
        await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert exc_info.value.code == "invalid_credentials"
    # Before the fix: `user is None` short-circuits the `or` -- `verify_password` is NEVER
    # called for an unknown username (0 calls). The fix must call it exactly once, against
    # a dummy hash, so the code path costs the same regardless of whether the user exists.
    assert calls == 1


# ---- (2) a locked account must not reveal its lock to a wrong password ---------------- #


async def test_locked_account_with_wrong_password_looks_like_invalid_credentials(
    session: AsyncSession,
) -> None:
    password = "C0rrect-Horse!"
    username = await _make_user(session, password=password)
    user = await user_repo.get_by_username(session, username)
    assert user is not None
    user.locked_until = utcnow() + dt.timedelta(minutes=15)
    await session.commit()

    body = LoginRequest(username=username, password="Totally-Wr0ng!")
    with pytest.raises(AuthError) as exc_info:
        await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    # Before the fix: the lock check runs BEFORE the password check, so a wrong password
    # against a locked account still leaks `account_locked`. The fix must make this
    # indistinguishable from any other wrong password: `invalid_credentials`.
    assert exc_info.value.code == "invalid_credentials"


async def test_locked_account_with_correct_password_still_reports_locked(
    session: AsyncSession,
) -> None:
    """Regression guard: the lock must still be enforced -- only its *visibility* to an
    attacker without the password changes, not whether it blocks a legitimate holder of the
    correct password."""
    password = "C0rrect-Horse!"
    username = await _make_user(session, password=password)
    user = await user_repo.get_by_username(session, username)
    assert user is not None
    user.locked_until = utcnow() + dt.timedelta(minutes=15)
    await session.commit()

    body = LoginRequest(username=username, password=password)
    with pytest.raises(AuthError) as exc_info:
        await login(_FakeRequest(), _FakeResponse(), body, session)  # type: ignore[arg-type]

    assert exc_info.value.code == "account_locked"
