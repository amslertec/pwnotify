"""M6: the OpenAPI docs (`/api/docs`) and schema (`/api/openapi.json`) must be off by default.

They publish the entire route map + schemas to anonymous callers. `PWNOTIFY_ENABLE_DOCS`
(default false) gates them: disabled -> the routes do not exist (404); enabled -> served.
"""

from __future__ import annotations

from unittest import mock

import httpx
import pytest
from app import main
from app.core.config import Settings


def _app(*, enable_docs: bool) -> httpx.ASGITransport:
    # A non-existent static_dir keeps the SPA catch-all unmounted, so a disabled docs route
    # is a clean 404 rather than the SPA shell.
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        enable_docs=enable_docs,
        static_dir="/nonexistent-pwnotify-static",
        runtime_db_password="x",
    )
    with mock.patch.object(main, "get_settings", lambda: settings):
        app = main.create_app()
    return httpx.ASGITransport(app=app)


@pytest.mark.parametrize("path", ["/api/docs", "/api/openapi.json"])
async def test_docs_disabled_by_default(path: str) -> None:
    async with httpx.AsyncClient(transport=_app(enable_docs=False), base_url="http://t") as c:
        resp = await c.get(path)
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/api/docs", "/api/openapi.json"])
async def test_docs_served_when_enabled(path: str) -> None:
    async with httpx.AsyncClient(transport=_app(enable_docs=True), base_url="http://t") as c:
        resp = await c.get(path)
    assert resp.status_code == 200
