"""I2: an unhandled 500 must still carry the security headers and leak no traceback.

`SecurityHeadersMiddleware` is the outermost user middleware, but Starlette's
`ServerErrorMiddleware` sits OUTSIDE it -- a genuinely unhandled exception returned from
there bypasses CSP/nosniff entirely. The fix catches unhandled exceptions below the header
middleware, so a clean generic 500 flows back out through it (headers applied), with no
traceback in the body.
"""

from __future__ import annotations

from unittest import mock

import httpx
from app import main
from app.core.config import Settings


async def test_unhandled_exception_response_has_security_headers_and_no_traceback() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        static_dir="/nonexistent-pwnotify-static",
        runtime_db_password="x",
    )
    with mock.patch.object(main, "get_settings", lambda: settings):
        app = main.create_app()

    @app.get("/_test_boom")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("kaboom-secret-internal-detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/_test_boom")

    assert resp.status_code == 500
    header_names = {k.lower() for k in resp.headers}
    assert "content-security-policy" in header_names
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    # No internal detail / traceback leaks to the client.
    assert "kaboom-secret-internal-detail" not in resp.text
    assert "Traceback" not in resp.text
