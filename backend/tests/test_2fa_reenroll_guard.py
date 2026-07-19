"""C1: a 2FA interim token must NOT permit re-enrollment while 2FA is already active.

Attack chain (closed here): password-only login of a 2FA-enabled account yields the short-lived
2FA interim token (`TWOFA_COOKIE`, type "2fa"). `get_enrolling_user` honours that token, so
before this fix `POST /2fa/setup` overwrote the stored TOTP secret (and flipped
`totp_enabled=False`); `POST /2fa/enable` with the attacker's code then minted a full session.
Re-enrollment is now rejected while `totp_enabled` is True (requires an authenticated `disable`
first). The tests confirm the interim token IS still accepted by `get_enrolling_user` (so the
rejection comes from the new endpoint guard, not from auth resolution) and that the stored
secret is left untouched.

The routes are `@limiter.limit`-decorated; slowapi rejects a duck-typed request unless the
limiter is disabled, so the autouse fixture disables it for direct calls (same pattern as
`test_invitation_flow.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from app.api.deps import TWOFA_COOKIE, get_enrolling_user
from app.api.routes.auth import two_factor_enable, two_factor_setup
from app.core.crypto import decrypt, encrypt
from app.core.errors import ForbiddenError
from app.core.security import create_2fa_token
from app.core.twofa import generate_secret
from app.models.user import AppUser
from app.schemas.auth import TwoFactorCode
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


async def _enabled_2fa_user(session: AsyncSession) -> tuple[AppUser, str, str]:
    """A local account with 2FA already enabled and a known encrypted secret."""
    secret = generate_secret()
    enc = encrypt(secret)
    user = AppUser(
        username=f"reenroll-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=False,
        totp_enabled=True,
        totp_secret=enc,
    )
    session.add(user)
    await session.flush()
    return user, secret, enc


async def test_setup_rejected_when_2fa_already_enabled(session: AsyncSession) -> None:
    user, _secret, enc = await _enabled_2fa_user(session)
    request = _FakeRequest({TWOFA_COOKIE: create_2fa_token(str(user.id))})

    # The interim token IS accepted by the resolver -- proves the rejection below is the new
    # endpoint guard, not an auth failure.
    enrolling = await get_enrolling_user(request, session)  # type: ignore[arg-type]
    assert enrolling.id == user.id

    with pytest.raises(ForbiddenError) as exc_info:
        await two_factor_setup(request, enrolling, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "twofa_already_enabled"

    await session.refresh(user)
    assert user.totp_secret == enc, "The stored TOTP secret must be untouched"
    assert user.totp_enabled is True


async def test_enable_rejected_when_2fa_already_enabled(session: AsyncSession) -> None:
    user, secret, enc = await _enabled_2fa_user(session)
    request = _FakeRequest({TWOFA_COOKIE: create_2fa_token(str(user.id))})
    enrolling = await get_enrolling_user(request, session)  # type: ignore[arg-type]

    # A *valid* current code -- proving the block is the guard, not a bad-code rejection.
    valid_code = pyotp.TOTP(secret).now()
    with pytest.raises(ForbiddenError) as exc_info:
        await two_factor_enable(
            request,  # type: ignore[arg-type]
            _FakeResponse(),  # type: ignore[arg-type]
            TwoFactorCode(code=valid_code),
            enrolling,
            session,
        )
    assert exc_info.value.code == "twofa_already_enabled"

    await session.refresh(user)
    assert user.totp_secret == enc


async def test_setup_still_allowed_for_not_yet_enrolled_user(session: AsyncSession) -> None:
    """Non-vacuous guard: a user WITHOUT 2FA can still start enrollment (secret gets stored,
    stays inactive until enable)."""
    user = AppUser(
        username=f"fresh-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="admin",
        is_sso=False,
        totp_enabled=False,
        totp_secret=None,
    )
    session.add(user)
    await session.flush()
    request = _FakeRequest({})  # no cookie needed; user passed directly

    out = await two_factor_setup(request, user, session)  # type: ignore[arg-type]
    assert out.secret  # a fresh secret was issued
    await session.refresh(user)
    assert user.totp_secret is not None
    assert decrypt(user.totp_secret) == out.secret
    assert user.totp_enabled is False
