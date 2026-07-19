"""TDD for Task 3 (L3, Security Phase 6): two concurrent unauthenticated `POST /setup/admin`
requests in the `count==0` window must NOT both create a superadmin. The fix serializes only
the unauthenticated first-setup path with a Postgres transaction-scoped advisory lock
(`pg_advisory_xact_lock`) -- it must NOT rely on a global unique constraint on
`role='superadmin'`, because multiple superadmins are intentionally allowed later via the
authenticated `create_superadmin` path.

The ordinary `session` fixture (see conftest.py) is savepoint-isolated on a single connection
and rolls back at the end of each test -- it cannot demonstrate a genuine two-connection race.
This test instead opens two INDEPENDENT sessions on `migrated_engine`, each on its own
connection, and runs both `create_admin` calls concurrently via `asyncio.gather` so the
underlying asyncpg queries actually interleave. Because it commits to the real (shared) test
database outside savepoint isolation, it cleans up everything it creates in a `finally` block,
respecting the `user_session -> app_user` foreign key (session rows are deleted first).

A plain `asyncio.gather` of the two attempts turned out to be flaky as a *proof of the race*:
depending on scheduling, one coroutine sometimes runs `count -> insert -> commit` to completion
before the other even sends its count query, so the pre-fix code can pass by luck. To make the
overlap deterministic, `_admin_count` is monkeypatched for the duration of this test to add a
short sleep AFTER reading the count and BEFORE returning it -- this reliably widens the window
in which both coroutines observe `count == 0` when the guard runs unprotected. It does not
weaken the proof once the fix is in place: with `pg_advisory_xact_lock` acquired at the top of
`create_admin` (before `_admin_count` is even called), the losing coroutine blocks on the lock
itself and never reaches the patched, sleeping `_admin_count` until the winner has already
committed and released it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from app.api.deps import limiter
from app.api.routes import setup as setup_routes
from app.api.routes.setup import AdminCreate, create_admin
from app.core.errors import ConflictError
from app.repositories import user_repo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


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


async def test_concurrent_first_setup_creates_exactly_one_superadmin(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuous proof: without `pg_advisory_xact_lock`, both coroutines read the
    `_admin_count() == 0` guard before either commits its INSERT (asyncpg runs each session
    on its own connection, and `asyncio.gather` starts both concurrently) -- so both would
    pass the guard and both would create a superadmin, making `len(oks) == 2`. With the
    advisory lock acquired at the top of `create_admin`, the second caller blocks until the
    first's transaction commits (releasing the lock), then re-evaluates the count under the
    lock and sees `1`, so it is rejected with `ConflictError(code="admin_exists")`."""

    async def _slow_admin_count(session: AsyncSession) -> int:
        # Deterministic race widener -- see the module docstring for why this is safe for
        # the post-fix (GREEN) case.
        n = await user_repo.count(session)
        await asyncio.sleep(0.05)
        return n

    monkeypatch.setattr(setup_routes, "_admin_count", _slow_admin_count)

    factory = async_sessionmaker(bind=migrated_engine, expire_on_commit=False, class_=AsyncSession)

    created_usernames = ["race-a", "race-b"]

    async def attempt(username: str) -> object:
        async with factory() as s:  # type: ignore[misc]
            body = AdminCreate(username=username, password="Str0ng!Passw0rd1")
            return await create_admin(body, _FakeResponse(), _FakeRequest(), s)  # type: ignore[arg-type]

    try:
        # Ensure a clean slate: earlier NON-savepoint tests are expected to clean up after
        # themselves, but guard against stray committed rows so `_admin_count() == 0` holds
        # at the start of this test regardless of run order.
        async with factory() as s:  # type: ignore[misc]
            await s.execute(text("DELETE FROM user_session"))
            await s.execute(text("DELETE FROM app_user"))
            await s.commit()

        results = await asyncio.gather(
            attempt(created_usernames[0]), attempt(created_usernames[1]), return_exceptions=True
        )

        oks = [r for r in results if not isinstance(r, Exception)]
        errs = [r for r in results if isinstance(r, Exception)]

        assert len(oks) == 1, f"expected exactly one winner, got {len(oks)}: {results}"
        assert len(errs) == 1
        assert isinstance(errs[0], ConflictError)
        assert errs[0].code == "admin_exists"

        async with factory() as s:  # type: ignore[misc]
            assert await user_repo.count(s) == 1
    finally:
        # Clean up so the shared, session-scoped test database is left as it was found --
        # delete `user_session` rows first (FK references `app_user`), then the user rows.
        # Both attempted usernames are covered regardless of which one(s) actually got
        # created (e.g. the un-fixed, RED-state code creates a session row for BOTH).
        async with factory() as s:  # type: ignore[misc]
            await s.execute(
                text(
                    "DELETE FROM user_session WHERE user_id IN "
                    "(SELECT id FROM app_user WHERE username = ANY(:names))"
                ),
                {"names": created_usernames},
            )
            await s.execute(
                text("DELETE FROM app_user WHERE username = ANY(:names)"),
                {"names": created_usernames},
            )
            await s.commit()
