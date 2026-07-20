"""OIDC form_post transport hardening + audit I4, proven over real HTTP.

With `response_mode=form_post` (RFC 9700 §4.3.1) Entra delivers the callback as a cross-site
POST, so the callback route must accept POST (and reject GET with 405). Both OIDC routes are
also rate-limited (audit I4): an unauthenticated outbound amplifier must return 429 once the
configured login limit is exceeded.

`slowapi` needs a real `starlette.requests.Request` to evaluate a limit, so these drive the
full `create_app()` ASGI app over `httpx.AsyncClient` + `httpx.ASGITransport` (pattern from
`test_auth_ratelimit.py`). No SSO configuration is needed: an empty-body POST callback short-
circuits to a `sso_error` redirect (302) before any token exchange, and the limiter check runs
in the decorator wrapper BEFORE the handler body, so 429 fires regardless of handler outcome.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from app.api.deps import limiter
from app.core.config import get_settings
from app.main import create_app
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


def _login_limit_count() -> int:
    return int(get_settings().login_rate_limit.split("/", 1)[0])


async def test_callback_accepts_post_and_rejects_get(migrated_engine: AsyncEngine) -> None:
    """The callback is POST-only now (form_post). A single POST with an empty body is accepted
    (redirects to sso_error, NOT a 405), while GET returns 405 Method Not Allowed."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        post_resp = await client.post("/api/auth/oidc/callback", data={})
        get_resp = await client.get("/api/auth/oidc/callback")

    assert post_resp.status_code != 405, "POST must be accepted on the form_post callback"
    assert post_resp.status_code in (302, 307), post_resp.status_code
    assert "sso_error=1" in post_resp.headers.get("location", "")
    assert get_resp.status_code == 405, "GET must no longer be allowed on the callback"


async def test_callback_post_returns_429_past_configured_limit(
    migrated_engine: AsyncEngine,
) -> None:
    app = create_app()
    limit = _login_limit_count()
    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        for _ in range(limit + 3):
            resp = await client.post("/api/auth/oidc/callback", data={})
            statuses.append(resp.status_code)

    assert 429 in statuses, f"OIDC callback rate limit never triggered -- statuses: {statuses}"
    assert statuses[:limit] == [302] * limit


async def test_login_get_returns_429_past_configured_limit(migrated_engine: AsyncEngine) -> None:
    """`/oidc/login` is rate-limited too (I4). SSO is unconfigured here, so the pre-limit status
    is irrelevant -- the limiter check fires in the wrapper before the handler body, so 429 must
    appear once the limit is exceeded regardless."""
    app = create_app()
    limit = _login_limit_count()
    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        for _ in range(limit + 3):
            resp = await client.get("/api/auth/oidc/login")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"OIDC login rate limit never triggered -- statuses: {statuses}"
