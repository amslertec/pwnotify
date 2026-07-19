"""L5: `/health` must not disclose the running version to an unauthenticated caller.

`/health` is hit by the Docker HEALTHCHECK and needs no auth by design -- but the
`version` field it used to include let anyone probe the deployed version without
credentials. The authenticated `GET /api/version` remains the legitimate source of that
information and is untouched here.

Drives the route function directly (it takes no arguments and touches no DB), matching
the simplest style available for this endpoint -- unlike the rate-limit tests, no real
ASGI transport is needed since `/health` carries no `@limiter.limit` decorator.
"""

from __future__ import annotations

from app.api.routes.health import health


async def test_health_response_has_no_version_field() -> None:
    body = await health()

    assert body == {"status": "ok"}
    assert "version" not in body
