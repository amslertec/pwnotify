"""F-01: an over-long client IP must not disable the account lockout or the audit trail.

Sister finding to H1 (`test_user_agent_truncation.py`), same failure class over a different
field. `audit_log.ip_address` and `user_session.ip_address` are both `varchar(64)`. Postgres
does NOT truncate an over-long value -- it REJECTS the INSERT with `value too long for type
character varying(64)`. In the login handler the failed-attempt counter and the
`LOGIN_FAILED`/`ACCOUNT_LOCKED` audit rows are persisted in ONE shared `session.commit()`, so
an over-long `request.client.host` failed the audit INSERT, rolled the whole transaction back,
and left `failed_login_count` un-persisted -- the lockout never fired and no audit entry was
written.

`request.client.host` becomes attacker-influenced once a trusted proxy is configured: uvicorn's
ProxyHeaders middleware overwrites `request.client` from `X-Forwarded-For` WITHOUT validating it
is an IP, so an attacker-controlled, arbitrarily long value reaches the varchar(64) column.

These tests drive the real route functions (not over HTTP) with real commits -- the varchar
rejection and the counter/audit persistence can only be observed across genuine commits, so
each attempt runs on its own `get_session_factory()` session exactly like a real request. The
`@limiter.limit` decorator is disabled (duck-typed request), same pattern as the H1 tests.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import TWOFA_COOKIE, limiter
from app.api.routes.auth import login, two_factor_verify
from app.core.crypto import encrypt
from app.core.security import create_2fa_token, hash_password
from app.core.twofa import generate_secret
from app.db.session import get_session_factory
from app.models.user import UserSession
from app.repositories import user_repo
from app.schemas.auth import LoginRequest, TwoFactorCode
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# 200 chars > the varchar(64) column width -- long enough that Postgres rejects the INSERT.
# Mirrors an attacker-supplied X-Forwarded-For value promoted to `request.client.host`.
LONG_IP = "203.0.113." + "9" * 200
PASSWORD = "C0rrect-Horse!Battery"


@pytest.fixture(autouse=True)
def _limiter_disabled() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeClient:
    """Duck-typed `request.client` -- the auth routes only read `.host`."""

    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Duck-typed Request -- the auth routes only read `.headers`/`.client`/`.cookies`."""

    def __init__(self, *, host: str, cookies: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = {"user-agent": "pytest"}
        self.client: object | None = _FakeClient(host)
        self.cookies: dict[str, str] = cookies or {}


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}
        self.deleted_cookies: set[str] = set()

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value

    def delete_cookie(self, name: str, **_: object) -> None:
        self.deleted_cookies.add(name)


def _uname(label: str) -> str:
    return f"f01-{label}-{uuid.uuid4().hex[:10]}"


async def _count_audit(session: AsyncSession, actor_id: int, action: str) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM audit_log WHERE actor_id = :a AND action = :act"),
                {"a": actor_id, "act": action},
            )
        ).scalar_one()
    )


async def _cleanup(user_id: int) -> None:
    async with get_session_factory()() as s:
        await s.execute(text("DELETE FROM audit_log WHERE actor_id = :a"), {"a": user_id})
        await s.execute(text("DELETE FROM user_session WHERE user_id = :a"), {"a": user_id})
        await s.execute(text("DELETE FROM app_user WHERE id = :a"), {"a": user_id})
        await s.commit()


async def test_password_login_lockout_survives_overlong_ip(
    migrated_engine: AsyncEngine,
) -> None:
    """Five wrong-password attempts with a 200-char client IP must still lock the account and
    leave five `LOGIN_FAILED` audit entries. Before the fix the shared commit rolled back on the
    varchar(64) rejection, so neither the lock nor any audit row was ever persisted (RED)."""
    async with get_session_factory()() as s:
        user = await user_repo.create(
            session=s, username=_uname("pw"), password_hash=hash_password(PASSWORD), role="admin"
        )
        await s.commit()
        assert user.id is not None
        user_id = user.id

    try:
        for _ in range(5):
            async with get_session_factory()() as s:
                request = _FakeRequest(host=LONG_IP)
                body = LoginRequest(username=user.username, password="Wr0ng-Password!")
                with contextlib.suppress(Exception):
                    await login(request, _FakeResponse(), body, s)  # type: ignore[arg-type]

        async with get_session_factory()() as s:
            locked = await user_repo.get(s, user_id)
            assert locked is not None
            assert locked.locked_until is not None, (
                "account was never locked -- the over-long IP rolled back the shared commit, so "
                "failed_login_count never persisted (RED before the fix)"
            )
            failed = await _count_audit(s, user_id, "auth.login_failed")
            assert failed == 5, f"expected 5 LOGIN_FAILED audit rows, got {failed}"
            assert await _count_audit(s, user_id, "auth.account_locked") == 1
    finally:
        await _cleanup(user_id)


async def test_2fa_lockout_survives_overlong_ip(migrated_engine: AsyncEngine) -> None:
    """Five wrong 2FA codes with a 200-char client IP must lock the account AND leave the audit
    entries. The 2FA path commits the counter separately (so the lock itself survived), but the
    `LOGIN_FAILED`/`ACCOUNT_LOCKED` audit rows shared the later commit that the varchar rejection
    rolled back -- zero audit rows before the fix (RED)."""
    async with get_session_factory()() as s:
        user = await user_repo.create(
            session=s, username=_uname("2fa"), password_hash=hash_password(PASSWORD), role="admin"
        )
        user.totp_enabled = True
        user.totp_secret = encrypt(generate_secret())
        await s.commit()
        assert user.id is not None
        user_id = user.id

    twofa_cookie = create_2fa_token(str(user_id))
    try:
        for _ in range(5):
            async with get_session_factory()() as s:
                request = _FakeRequest(host=LONG_IP, cookies={TWOFA_COOKIE: twofa_cookie})
                body = TwoFactorCode(code="000000")  # never a valid TOTP for a fresh secret
                with contextlib.suppress(Exception):
                    await two_factor_verify(request, _FakeResponse(), body, s)  # type: ignore[arg-type]

        async with get_session_factory()() as s:
            locked = await user_repo.get(s, user_id)
            assert locked is not None
            assert locked.locked_until is not None, "2FA guessing must still lock the account"
            failed = await _count_audit(s, user_id, "auth.login_failed")
            assert failed == 5, (
                f"expected 5 LOGIN_FAILED audit rows for the 2FA path, got {failed} -- the "
                "over-long IP rolled back the audit commit (RED before the fix)"
            )
            assert await _count_audit(s, user_id, "auth.account_locked") == 1
    finally:
        await _cleanup(user_id)


async def test_complete_login_survives_overlong_ip(
    migrated_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A successful login carrying a 200-char client IP must succeed and create a `user_session`
    row whose `ip_address` fits the varchar(64) column. Before the fix `_complete_login`'s
    `create_session` INSERT was rejected outright (collateral 500)."""
    from app.api.routes.auth import _complete_login

    user = await user_repo.create(
        session=session, username=_uname("ok"), password_hash=hash_password(PASSWORD), role="admin"
    )
    await session.commit()
    assert user.id is not None

    request = _FakeRequest(host=LONG_IP)
    await _complete_login(request, _FakeResponse(), session, user)  # type: ignore[arg-type]

    us = (
        await session.execute(select(UserSession).where(UserSession.user_id == user.id))
    ).scalar_one()
    assert us.ip_address is not None
    assert len(us.ip_address) <= 64, (
        f"user_session.ip_address was not truncated to the column width: {len(us.ip_address)}"
    )
