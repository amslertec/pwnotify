"""L7: real HTTP proof that `/auth/password`, `/auth/refresh`, and `/auth/activity` are
rate-limited.

Follows `test_public_tokens_ratelimit.py` exactly: `slowapi` needs a real
`starlette.requests.Request` (client IP via `get_remote_address`) to evaluate a limit, so a
direct route-function call (as most other auth tests do) proves nothing here. This drives
the full `create_app()` ASGI app over `httpx.AsyncClient` + `httpx.ASGITransport` instead --
no lifespan needed, the `migrated_engine` fixture already points `PWNOTIFY_DATABASE_URL` at
a migrated test database.

`/auth/refresh` and `/auth/activity` need no auth at all to reach the limiter (both read an
absent/invalid cookie and fail generically), so both are driven exactly like the existing
public-tokens test: past `auth_refresh_rate_limit` calls, `429` must appear.

`/auth/password` requires `CurrentUser`, which FastAPI resolves as a dependency BEFORE
calling the (limiter-decorated) endpoint function -- an invalid/missing session would
reject with 401 in dependency resolution, never reaching the `@limiter.limit` check inside
the wrapped function body at all, making the test vacuous. So this drives it with a real,
committed `AppUser` row and a genuine access-token cookie (pattern from
`test_admin_user_avatar.py`'s `avatar_route_users`/`issue_token_pair`) submitting a
deliberately wrong `current_password` on every call -- the dependency resolves fine, the
limiter check runs first inside the handler, and only then does `wrong_current_password`
(401) get a chance to fire. The limit must still win once exceeded.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator

import httpx
import pytest
import pytest_asyncio
from app.api.deps import ACCESS_COOKIE, limiter
from app.core.config import get_settings
from app.core.security import hash_password, issue_token_pair
from app.main import create_app
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(autouse=True)
def _rate_limiter_enabled_and_reset() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.enabled = prev


def _refresh_limit_count() -> int:
    return int(get_settings().auth_refresh_rate_limit.split("/", 1)[0])


def _login_limit_count() -> int:
    return int(get_settings().login_rate_limit.split("/", 1)[0])


@pytest_asyncio.fixture
async def password_route_user(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """One real, committed local account -- a genuine access-token cookie for it lets the
    `CurrentUser` dependency resolve so the request actually reaches the limiter-decorated
    handler body (see module docstring)."""
    async with migrated_engine.connect() as conn:
        uid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO app_user (username, password_hash, role, is_active, "
                        "is_sso, failed_login_count, language, created_at, updated_at) "
                        "VALUES (:username, :hash, 'superadmin', true, false, 0, 'de', "
                        "now(), now()) RETURNING id"
                    ),
                    {
                        "username": f"pwlimit-{uuid.uuid4().hex[:8]}",
                        "hash": hash_password("Correct!Horse9Battery"),
                    },
                )
            ).scalar_one()
        )
        await conn.commit()
        try:
            yield uid
        finally:
            await conn.execute(text("DELETE FROM app_user WHERE id = :uid"), {"uid": uid})
            await conn.commit()


async def test_auth_refresh_returns_429_past_configured_limit(
    migrated_engine: AsyncEngine,
) -> None:
    app = create_app()
    limit = _refresh_limit_count()

    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(limit + 3):
            resp = await client.post("/api/auth/refresh")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Rate limit never triggered -- status sequence: {statuses}"
    assert all(s in (401, 429) for s in statuses)
    assert statuses[:limit] == [401] * limit


async def test_auth_activity_returns_429_past_configured_limit(
    migrated_engine: AsyncEngine,
) -> None:
    app = create_app()
    limit = _refresh_limit_count()

    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(limit + 3):
            resp = await client.post("/api/auth/activity")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Rate limit never triggered -- status sequence: {statuses}"
    assert all(s in (401, 429) for s in statuses)
    assert statuses[:limit] == [401] * limit


async def test_auth_password_returns_429_past_configured_limit(
    migrated_engine: AsyncEngine, password_route_user: int
) -> None:
    app = create_app()
    limit = _login_limit_count()
    token = issue_token_pair(str(password_route_user)).access_token

    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(limit + 3):
            resp = await client.post(
                "/api/auth/password",
                json={"current_password": "wrong-password", "new_password": "Str0ng!Passw0rd"},
                cookies={ACCESS_COOKIE: token},
            )
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Rate limit never triggered -- status sequence: {statuses}"
    assert all(s in (401, 429) for s in statuses)
    assert statuses[:limit] == [401] * limit
