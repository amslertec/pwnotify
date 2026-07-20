"""F-02: the current-password reauth on `/auth/password` must count failures + lock + audit,
the same brute-force protection login and 2FA-disable have.

`change_password` used to reject a wrong `current_password` with a bare `AuthError` -- no
failed-attempt counter, no lockout, no audit trail. A hijacked session could therefore guess
the plaintext password (bounded only by the per-IP rate limit) and then take the account over
by setting a new one. The fix mirrors the login handler: a wrong password is counted, audited
and committed, and can lock the account; a locked account is refused up front.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.routes.auth import change_password
from app.core.errors import AuthError
from app.core.security import hash_password, verify_password
from app.models.audit import AuditLog
from app.repositories import user_repo
from app.schemas.auth import PasswordChangeRequest
from app.services import audit
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_CURRENT = "Curr3nt!Pass99"
_NEW = "Str0ng!Pass99"


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


async def _local_user(session: AsyncSession, username: str) -> object:
    return await user_repo.create(
        session,
        username=username,
        password_hash=hash_password(_CURRENT),
        role="admin",
    )


async def test_wrong_current_password_counts_failures_and_locks_and_audits(
    session: AsyncSession,
) -> None:
    """Five wrong-current-password attempts MUST lock the account and leave an audit trail;
    a further attempt -- even with the correct password -- is then refused as locked."""
    user = await _local_user(session, "pw-reauth-lock")

    for _ in range(5):  # login_max_failures default = 5
        with pytest.raises(AuthError) as exc_info:
            await change_password(
                request=_FakeRequest(),  # type: ignore[arg-type]
                body=PasswordChangeRequest(current_password="wrong-password", new_password=_NEW),
                user=user,  # type: ignore[arg-type]
                session=session,
                response=_FakeResponse(),  # type: ignore[arg-type]
            )
        assert exc_info.value.code == "wrong_current_password"

    await session.refresh(user)  # type: ignore[arg-type]
    assert user.locked_until is not None  # type: ignore[attr-defined]
    # The password was never rotated.
    assert verify_password(_CURRENT, user.password_hash)  # type: ignore[attr-defined]

    # Locked out now -- the correct password no longer helps.
    with pytest.raises(AuthError) as exc_info:
        await change_password(
            request=_FakeRequest(),  # type: ignore[arg-type]
            body=PasswordChangeRequest(current_password=_CURRENT, new_password=_NEW),
            user=user,  # type: ignore[arg-type]
            session=session,
            response=_FakeResponse(),  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "account_locked"

    failed = (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.actor_id == user.id, AuditLog.action == audit.LOGIN_FAILED)  # type: ignore[attr-defined]
        )
    ).scalar_one()
    locked = (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.actor_id == user.id, AuditLog.action == audit.ACCOUNT_LOCKED)  # type: ignore[attr-defined]
        )
    ).scalar_one()
    assert failed == 5
    assert locked == 1


async def test_successful_change_resets_failed_counter(session: AsyncSession) -> None:
    """A successful change clears a partial failure counter, mirroring the login path's
    reset_failed_attempts -- otherwise stale failures would lock the account early."""
    user = await _local_user(session, "pw-reauth-reset")
    user.failed_login_count = 3  # type: ignore[attr-defined]
    await session.flush()

    await change_password(
        request=_FakeRequest(),  # type: ignore[arg-type]
        body=PasswordChangeRequest(current_password=_CURRENT, new_password=_NEW),
        user=user,  # type: ignore[arg-type]
        session=session,
        response=_FakeResponse(),  # type: ignore[arg-type]
    )
    await session.refresh(user)  # type: ignore[arg-type]
    assert user.failed_login_count == 0  # type: ignore[attr-defined]
    assert verify_password(_NEW, user.password_hash)  # type: ignore[attr-defined]
