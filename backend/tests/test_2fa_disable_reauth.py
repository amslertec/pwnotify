"""L1: disabling 2FA must re-authenticate with the password AND consume the TOTP step.

`two_factor_disable` used to accept the code via `verify_totp(...)` -- which never records
`totp_last_step` -- and required no password at all. Two consequences:

1. A TOTP code captured during login (shoulder-surf, recording) stayed valid for its ~90 s
   window, so the same code could log in AND then permanently switch off the second factor.
   The canonical verify/enable paths burn the step in `totp_last_step`; disable did not.
2. A hijacked session alone could turn off 2FA, because no password re-auth was required.

The fix: disable now (a) re-authenticates with the account password and (b) consumes the
matched step exactly like `/2fa/verify`, rejecting a replay of the already-used step.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pyotp
import pytest
from app.api.routes.auth import two_factor_disable
from app.core.crypto import encrypt
from app.core.errors import AuthError
from app.core.security import hash_password
from app.core.twofa import generate_recovery_codes, generate_secret, matching_step
from app.models.user import AppUser
from app.schemas.auth import TwoFactorDisable, UserOut
from sqlalchemy.ext.asyncio import AsyncSession

_PASSWORD = "Str0ng!Pass99"


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
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


async def _twofa_user(session: AsyncSession) -> tuple[AppUser, str, list[str]]:
    """A local account with 2FA active: secret stored, real password hash, recovery codes."""
    secret = generate_secret()
    codes, hashes = generate_recovery_codes()
    user = AppUser(
        username=f"disable-reauth-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password(_PASSWORD),
        role="admin",
        is_sso=False,
        totp_enabled=True,
        totp_secret=encrypt(secret),
        totp_last_step=None,
        recovery_codes=json.dumps(hashes),
    )
    session.add(user)
    await session.flush()
    return user, secret, codes


async def test_replayed_login_code_is_rejected_at_disable(session: AsyncSession) -> None:
    """A code already consumed at login (its step burned) must not disable 2FA (L1 replay)."""
    user, secret, _ = await _twofa_user(session)
    code = pyotp.TOTP(secret).now()
    # Simulate the login having consumed exactly this step.
    user.totp_last_step = matching_step(secret, code)
    await session.flush()

    with pytest.raises(AuthError) as exc_info:
        await two_factor_disable(
            _FakeRequest(),  # type: ignore[arg-type]
            TwoFactorDisable(code=code, password=_PASSWORD),
            user,
            session,
            None,
        )
    assert exc_info.value.code == "invalid_2fa_code"
    await session.refresh(user)
    assert user.totp_enabled is True  # 2FA stays on


async def test_wrong_password_is_rejected_even_with_fresh_code(session: AsyncSession) -> None:
    """A fresh, valid code with the WRONG password must not disable 2FA (L1 re-auth)."""
    user, secret, _ = await _twofa_user(session)
    fresh_code = pyotp.TOTP(secret).now()

    with pytest.raises(AuthError) as exc_info:
        await two_factor_disable(
            _FakeRequest(),  # type: ignore[arg-type]
            TwoFactorDisable(code=fresh_code, password="wrong-password"),
            user,
            session,
            None,
        )
    assert exc_info.value.code == "invalid_password"
    await session.refresh(user)
    assert user.totp_enabled is True


async def test_fresh_code_and_correct_password_disables(session: AsyncSession) -> None:
    """Happy path: fresh code + correct password turns 2FA off and clears the secret."""
    user, secret, _ = await _twofa_user(session)
    fresh_code = pyotp.TOTP(secret).now()

    out = await two_factor_disable(
        _FakeRequest(),  # type: ignore[arg-type]
        TwoFactorDisable(code=fresh_code, password=_PASSWORD),
        user,
        session,
        None,
    )
    assert isinstance(out, UserOut)
    await session.refresh(user)
    assert user.totp_enabled is False
    assert user.totp_secret is None
    assert user.recovery_codes is None


async def test_recovery_code_with_correct_password_disables(session: AsyncSession) -> None:
    """A recovery code stays a valid fallback -- but still needs the correct password."""
    user, _, codes = await _twofa_user(session)

    out = await two_factor_disable(
        _FakeRequest(),  # type: ignore[arg-type]
        TwoFactorDisable(code=codes[0], password=_PASSWORD),
        user,
        session,
        None,
    )
    assert isinstance(out, UserOut)
    await session.refresh(user)
    assert user.totp_enabled is False
