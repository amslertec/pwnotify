"""L2: the code used to enable 2FA must be burned so it cannot be replayed at `/2fa/verify`.

`two_factor_enable` used to check the enrollment code with `verify_totp(...)` and never
recorded `user.totp_last_step`. The canonical verify path (`two_factor_verify`) uses
`matching_step(...)` and stores the consumed step in `totp_last_step`, rejecting a repeat
of the same step as a replay -- but since enable never wrote that field, the exact code a
user typed to enable 2FA stayed valid at `/2fa/verify` for its ~30-90 s TOTP window. Fix:
`two_factor_enable` now consumes the step the same way `two_factor_verify` does.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pyotp
import pytest
from app.api.deps import TWOFA_COOKIE
from app.api.routes.auth import two_factor_enable, two_factor_verify
from app.core.crypto import encrypt
from app.core.errors import AuthError
from app.core.security import create_2fa_token
from app.core.twofa import generate_secret
from app.models.user import AppUser
from app.schemas.auth import RecoveryCodesOut, TwoFactorCode
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
    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies
        self.headers: dict[str, str] = {}
        self.client: object | None = None


class _FakeResponse:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def set_cookie(self, name: str, value: str, **_: object) -> None:  # pragma: no cover
        pass

    def delete_cookie(self, name: str, **_: object) -> None:  # pragma: no cover
        self.deleted.append(name)


async def _enrolling_user(session: AsyncSession) -> tuple[AppUser, str]:
    """A local account mid-enrollment: secret stored, 2FA not yet active."""
    secret = generate_secret()
    user = AppUser(
        username=f"enable-replay-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=False,
        totp_enabled=False,
        totp_secret=encrypt(secret),
        totp_last_step=None,
    )
    session.add(user)
    await session.flush()
    return user, secret


async def test_enable_consumed_code_is_rejected_at_verify(session: AsyncSession) -> None:
    user, secret = await _enrolling_user(session)
    code = pyotp.TOTP(secret).now()

    request_without_access_cookie = _FakeRequest({})
    out = await two_factor_enable(
        request_without_access_cookie,  # type: ignore[arg-type]
        _FakeResponse(),  # type: ignore[arg-type]
        TwoFactorCode(code=code),
        user,
        session,
    )
    assert isinstance(out, RecoveryCodesOut)

    await session.refresh(user)
    assert user.totp_enabled is True
    # Core assertion: the enrollment code must be burned, or it is replayable at /2fa/verify.
    assert user.totp_last_step is not None

    # Replay the SAME code at verify -- must be rejected.
    verify_request = _FakeRequest({TWOFA_COOKIE: create_2fa_token(str(user.id))})
    with pytest.raises(AuthError) as exc_info:
        await two_factor_verify(
            verify_request,  # type: ignore[arg-type]
            _FakeResponse(),  # type: ignore[arg-type]
            TwoFactorCode(code=code),
            session,
        )
    assert exc_info.value.code == "invalid_2fa_code"


async def test_fresh_code_at_verify_still_succeeds(session: AsyncSession) -> None:
    """Non-vacuous companion: the guard blocks only the replayed step, not all codes."""
    user, secret = await _enrolling_user(session)
    enable_code = pyotp.TOTP(secret).now()

    request_without_access_cookie = _FakeRequest({})
    await two_factor_enable(
        request_without_access_cookie,  # type: ignore[arg-type]
        _FakeResponse(),  # type: ignore[arg-type]
        TwoFactorCode(code=enable_code),
        user,
        session,
    )
    await session.refresh(user)
    assert user.totp_last_step is not None

    # A code from a clearly different step must still be accepted.
    fresh_code = pyotp.TOTP(secret).at(time.time() + 30)
    verify_request = _FakeRequest({TWOFA_COOKIE: create_2fa_token(str(user.id))})
    result = await two_factor_verify(
        verify_request,  # type: ignore[arg-type]
        _FakeResponse(),  # type: ignore[arg-type]
        TwoFactorCode(code=fresh_code),
        session,
    )
    assert result is not None
