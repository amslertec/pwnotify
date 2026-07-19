"""TDD for Security Phase 5, Task 2 (M3): `password_meets_policy` (`core/security.py`) is
enforced at EVERY path that sets a real, user-chosen password -- not just the invite/reset
flow in `public_tokens.py` (already enforced there). This module drives the four remaining
direct-set paths and proves each rejects a policy-violating password with `ForbiddenError
code="password_policy"` while still accepting a compliant one:

- `setup.create_admin` (first-time-setup superadmin)
- `auth.change_password` (`/auth/password`)
- `admin_users.create_local` (direct mode, `raw_password is not None`)
- `admin_users.create_superadmin` (direct mode, `raw_password is not None`)

Non-vacuous: against the unfixed code these all pass through to `hash_password` (pydantic's
`min_length=10` on the schema lets a 10+ char, all-lowercase password through), so each test
fails RED before the fix and passes GREEN after.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import limiter
from app.api.routes.admin_users import create_local, create_superadmin
from app.api.routes.auth import change_password
from app.api.routes.setup import AdminCreate, create_admin
from app.core.errors import ForbiddenError
from app.core.security import hash_password, verify_password
from app.models.user import AppUser
from app.repositories import user_repo
from app.schemas.auth import AdminUserCreate, PasswordChangeRequest, SuperadminCreate
from sqlalchemy.ext.asyncio import AsyncSession

_WEAK_PASSWORD = "alllowercase1"  # 13 chars, no upper, no special -- passes min_length, not policy
_STRONG_PASSWORD = "Str0ng!Pass99"


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = prev


class _FakeRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client: object | None = None
        self.cookies: dict[str, str] = {}


class _FakeResponse:
    def __init__(self) -> None:
        self.cookie_values: dict[str, str] = {}

    def set_cookie(self, name: str, value: str, **_: object) -> None:
        self.cookie_values[name] = value


async def _mk_superadmin(session: AsyncSession, *, username: str) -> AppUser:
    u = AppUser(username=username, password_hash="x", role="superadmin", is_sso=False)
    session.add(u)
    await session.flush()
    return u


# ---- setup.create_admin (first-time-setup superadmin) --------------------------------- #


async def test_setup_create_admin_rejects_weak_password(session: AsyncSession) -> None:
    body = AdminCreate(username="policy-setup-weak", password=_WEAK_PASSWORD)
    with pytest.raises(ForbiddenError) as exc_info:
        await create_admin(body, _FakeResponse(), _FakeRequest(), session)  # type: ignore[arg-type]
    assert exc_info.value.code == "password_policy"


async def test_setup_create_admin_accepts_strong_password(session: AsyncSession) -> None:
    body = AdminCreate(username="policy-setup-strong", password=_STRONG_PASSWORD)
    out = await create_admin(body, _FakeResponse(), _FakeRequest(), session)  # type: ignore[arg-type]
    assert out.role == "superadmin"


# ---- auth.change_password (/auth/password) --------------------------------------------- #


async def test_change_password_rejects_weak_new_password(session: AsyncSession) -> None:
    current = "Curr3nt!Pass99"
    user = await user_repo.create(
        session,
        username="policy-change-weak",
        password_hash=hash_password(current),
        role="admin",
    )
    body = PasswordChangeRequest(current_password=current, new_password=_WEAK_PASSWORD)
    with pytest.raises(ForbiddenError) as exc_info:
        await change_password(
            request=_FakeRequest(),  # type: ignore[arg-type]
            body=body,
            user=user,
            session=session,
            response=_FakeResponse(),  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "password_policy"
    # Rejection must not silently rotate the hash.
    await session.refresh(user)
    assert verify_password(current, user.password_hash)


async def test_change_password_accepts_strong_new_password(session: AsyncSession) -> None:
    current = "Curr3nt!Pass99"
    user = await user_repo.create(
        session,
        username="policy-change-strong",
        password_hash=hash_password(current),
        role="admin",
    )
    body = PasswordChangeRequest(current_password=current, new_password=_STRONG_PASSWORD)
    await change_password(
        request=_FakeRequest(),  # type: ignore[arg-type]
        body=body,
        user=user,
        session=session,
        response=_FakeResponse(),  # type: ignore[arg-type]
    )
    await session.refresh(user)
    assert verify_password(_STRONG_PASSWORD, user.password_hash)


# ---- admin_users.create_local (direct mode) --------------------------------------------- #


async def test_create_local_rejects_weak_password(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session, username="policy-local-caller-weak")
    body = AdminUserCreate(username="policy-local-weak", password=_WEAK_PASSWORD, role="admin")
    with pytest.raises(ForbiddenError) as exc_info:
        await create_local(None, caller, body, session, None)  # type: ignore[arg-type]
    assert exc_info.value.code == "password_policy"
    assert await user_repo.get_by_username(session, "policy-local-weak") is None


async def test_create_local_accepts_strong_password(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session, username="policy-local-caller-strong")
    body = AdminUserCreate(username="policy-local-strong", password=_STRONG_PASSWORD, role="admin")
    out = await create_local(None, caller, body, session, None)  # type: ignore[arg-type]
    assert out.username == "policy-local-strong"


# ---- admin_users.create_superadmin (direct mode) ---------------------------------------- #


async def test_create_superadmin_rejects_weak_password(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session, username="policy-super-caller-weak")
    body = SuperadminCreate(username="policy-super-weak", password=_WEAK_PASSWORD)
    with pytest.raises(ForbiddenError) as exc_info:
        await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "password_policy"
    assert await user_repo.get_by_username(session, "policy-super-weak") is None


async def test_create_superadmin_accepts_strong_password(session: AsyncSession) -> None:
    caller = await _mk_superadmin(session, username="policy-super-caller-strong")
    body = SuperadminCreate(username="policy-super-strong", password=_STRONG_PASSWORD)
    out = await create_superadmin(None, caller, body, session)  # type: ignore[arg-type]
    assert out.username == "policy-super-strong"
