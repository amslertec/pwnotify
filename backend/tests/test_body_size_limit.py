"""M5: an ASGI body-size guard must reject over-large requests before any handler reads them.

The per-route 2/5 MB upload checks only run AFTER `await file.read()` has already spooled the
whole body to disk -- a multi-GB body is written out before any limit fires. `MaxBodySizeMiddleware`
rejects at the transport layer: a present Content-Length is trusted (rejected before a single
byte is read), and streamed bytes are counted as a fallback for chunked bodies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest import mock

import httpx
from app import main
from app.core.body_limit import MaxBodySizeMiddleware
from app.core.config import Settings


def _app_with_limit(max_bytes: int) -> httpx.ASGITransport:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        max_request_body_bytes=max_bytes,
        static_dir="/nonexistent-pwnotify-static",
        runtime_db_password="x",
    )
    with mock.patch.object(main, "get_settings", lambda: settings):
        app = main.create_app()
    return httpx.ASGITransport(app=app)


async def test_oversize_content_length_rejected_with_413() -> None:
    async with httpx.AsyncClient(transport=_app_with_limit(1024), base_url="http://t") as c:
        # 2 KB body, Content-Length set by httpx -> guard rejects before routing/auth.
        resp = await c.post("/api/me/avatar", content=b"x" * 2048)
    assert resp.status_code == 413
    # The 413 still carries the security headers (flows back through SecurityHeadersMiddleware).
    assert "content-security-policy" in {k.lower() for k in resp.headers}


async def test_small_request_passes_the_guard() -> None:
    async with httpx.AsyncClient(transport=_app_with_limit(1024), base_url="http://t") as c:
        resp = await c.post("/api/me/avatar", content=b"x" * 100)
    # Not rejected by the size guard -- it reaches auth instead (401, no cookie). The point is
    # that a small legitimate request is untouched by the middleware.
    assert resp.status_code != 413


async def test_streaming_body_over_limit_rejected() -> None:
    """Chunked upload (no Content-Length) is rejected once the counted bytes exceed the limit."""

    async def echo(scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
        while True:
            message = await receive()  # type: ignore[operator]
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    app = MaxBodySizeMiddleware(echo, max_bytes=1024)
    transport = httpx.ASGITransport(app=app)

    async def big() -> AsyncIterator[bytes]:
        yield b"x" * 4096

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        big_resp = await c.post("/", content=big())
        small_resp = await c.post("/", content=b"x" * 100)

    assert big_resp.status_code == 413
    assert small_resp.status_code == 200
