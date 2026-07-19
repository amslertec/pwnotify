"""M5: branding uploads must be isolated per tenant on disk.

Before this fix, `_branding_dir()` returned a single shared, NON-tenant-scoped directory
(`{data_dir}/branding`) and `_save_upload` wrote under a fixed stem (`"logo"`/`"favicon"`)
inside it. The `branding.logo_path`/`branding.favicon_path` *setting values* are already
tenant-scoped (Phase 3), but the underlying *file* was not: tenant A and tenant B both
resolved to the exact same path (`{data_dir}/branding/logo.<ext>`), so tenant B's upload
silently deleted/overwrote tenant A's logo file on disk (cross-tenant clobber), even though
each tenant's own `branding.logo_path` setting row stayed intact.

Real-commit pattern (two real tenants, real Postgres) mirroring `test_audit_tenant_scope.py`
-- the fix relies on the `current_tenant_or_none()` ContextVar, which only a real
`tenant_scoped_session` binds; the savepoint-`session` fixture does not exercise that path.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio
from app.api.routes import branding
from app.core.config import get_settings
from app.db.tenant_context import tenant_scoped_session
from app.services.settings_service import SettingsService
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers

# Harmless SVGs (as in test_branding_tenant_scope.py) -- no Pillow decode involved, keeps
# this test focused on path isolation rather than image processing.
LOGO_A = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <circle cx="5" cy="5" r="4" fill="#111111"/>
</svg>"""
LOGO_B = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <circle cx="5" cy="5" r="4" fill="#222222"/>
</svg>"""


def _upload_file(data: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename="logo.svg",
        headers=Headers({"content-type": "image/svg+xml"}),
    )


@pytest_asyncio.fixture
async def tmp_data_dir(tmp_path) -> AsyncGenerator[None]:
    """Branding uploads land under `{data_dir}/branding` -- redirect to a throwaway
    directory for the test, same ENV-override pattern as `migrated_engine` in conftest.py."""
    prev = os.environ.get("PWNOTIFY_DATA_DIR")
    os.environ["PWNOTIFY_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield
    if prev is None:
        os.environ.pop("PWNOTIFY_DATA_DIR", None)
    else:
        os.environ["PWNOTIFY_DATA_DIR"] = prev
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def two_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[dict[str, int]]:
    """Two real, committed tenants -- FK-safe cleanup in `finally`."""
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                "('BrandA','brand-a',true,now()), ('BrandB','brand-b',true,now())"
            )
        )
        a, b = (
            (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE slug IN ('brand-a','brand-b') ORDER BY slug")
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        try:
            yield {"a": int(a), "b": int(b)}
        finally:
            # `branding.changed` audit rows (Security Phase 5, Task 8/M10) reference these
            # tenants via a plain FK (no ON DELETE) -- clear them before the tenant rows.
            await conn.execute(
                text("DELETE FROM audit_log WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
            )
            await conn.execute(
                text("DELETE FROM setting WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}
            )
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


def _assert_file_contains(path: str, expected: bytes, label: str) -> None:
    """Plain (non-async) helper -- keeps the blocking `pathlib` I/O out of the async test
    body (ruff ASYNC240)."""
    file = Path(path)
    assert file.is_file(), f"{label}: file no longer exists"
    assert file.read_bytes() == expected, f"{label}: file content was overwritten"


async def test_second_tenant_upload_does_not_overwrite_first_tenants_logo_file(
    tmp_data_dir: None, two_tenants: dict[str, int]
) -> None:
    a, b = two_tenants["a"], two_tenants["b"]

    async with tenant_scoped_session(a) as session_a:
        svc_a = SettingsService(session_a)
        await branding.upload_logo(None, None, svc_a, session_a, _upload_file(LOGO_A))  # type: ignore[arg-type]
        path_a = await svc_a.get("branding.logo_path")

    async with tenant_scoped_session(b) as session_b:
        svc_b = SettingsService(session_b)
        await branding.upload_logo(None, None, svc_b, session_b, _upload_file(LOGO_B))  # type: ignore[arg-type]
        path_b = await svc_b.get("branding.logo_path")

    assert path_a is not None
    assert path_b is not None
    assert path_a != path_b, (
        "Tenant A and tenant B resolved to the same storage path -- cross-tenant overwrite"
    )

    _assert_file_contains(path_a, LOGO_A, "Tenant A's logo")
    _assert_file_contains(path_b, LOGO_B, "Tenant B's logo")
