"""C3: the setup test-endpoints (`database/test`, `graph/test`, `mail/test`) must be closed on a
provisioned instance (>=1 admin) while staying open during first-time setup (0 admins). Before
this fix the whole `/setup` router was ungated -- an unauthenticated caller could probe Graph,
send real mail, and read raw DB exceptions.

The security lives in the shared guard `_require_setup_open_or_admin`, tested directly here:
- provisioned + unauthenticated  -> AuthError (401)
- provisioned + authenticated admin -> passes (returns None)
- first-setup (0 admins)         -> passes (returns None)

Also verifies `database_test` no longer leaks the raw exception string. Uses the savepoint
`session` fixture (each test starts with 0 admins). The limiter is disabled for the direct
`database_test` call (slowapi requires a real Request otherwise)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.routes.setup import _require_setup_open_or_admin, database_test
from app.core.errors import AuthError
from app.core.security import issue_token_pair
from app.repositories import user_repo
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
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


async def _make_admin(session: AsyncSession) -> Any:
    return await user_repo.create(
        session,
        username=f"prov-admin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="superadmin",
        is_sso=False,
    )


async def test_guard_blocks_unauthenticated_on_provisioned_instance(session: AsyncSession) -> None:
    await _make_admin(session)  # now _admin_count > 0
    with pytest.raises(AuthError) as exc_info:
        await _require_setup_open_or_admin(_FakeRequest(), session)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 401


async def test_guard_allows_authenticated_admin_on_provisioned_instance(
    session: AsyncSession,
) -> None:
    admin = await _make_admin(session)
    from app.api.deps import ACCESS_COOKIE

    request = _FakeRequest({ACCESS_COOKIE: issue_token_pair(str(admin.id)).access_token})
    # Must NOT raise.
    assert await _require_setup_open_or_admin(request, session) is None  # type: ignore[arg-type]


async def test_guard_open_during_first_setup(session: AsyncSession) -> None:
    # 0 admins in the savepoint-isolated DB -> open.
    assert await _require_setup_open_or_admin(_FakeRequest(), session) is None  # type: ignore[arg-type]


async def test_database_test_error_is_generic(session: AsyncSession) -> None:
    """On a DB failure the raw exception must not leak (was `error=str(exc)`)."""

    class _BoomSession:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("postgresql://secret:dsn@host/db boom")

    out = await database_test(_FakeRequest(), _BoomSession(), None)  # type: ignore[arg-type]
    assert out.connected is False
    assert out.error is not None
    assert "postgresql://" not in out.error
    assert "secret" not in out.error
