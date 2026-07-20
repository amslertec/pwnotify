"""M6: `/ready` must be rate-limited.

`/ready` is unauthenticated and opens a DB session (SELECT 1). Without a limit, a flood
exhausts the connection pool (workers=1) and takes the app down. `/health` stays
deliberately limit-free/DB-free for the Docker HEALTHCHECK -- only `/ready` is capped here.

Drives the full `create_app()` ASGI app over a real transport, because slowapi needs a real
`starlette.requests.Request` (client IP via `get_remote_address`) to evaluate the limit --
a bare function call does not exercise it. Same approach as `test_public_tokens_ratelimit.py`.
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


def _configured_limit_count() -> int:
    return int(get_settings().ready_rate_limit.split("/", 1)[0])


async def test_ready_returns_429_past_configured_limit(migrated_engine: AsyncEngine) -> None:
    app = create_app()
    limit = _configured_limit_count()

    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(limit + 3):
            resp = await client.get("/ready")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Rate-Limit hat nie gegriffen -- Status-Folge: {statuses}"
    # Up to the limit the endpoint answers normally (200), then 429. Nothing else may appear.
    assert all(s in (200, 429) for s in statuses)
    assert statuses[:limit] == [200] * limit
