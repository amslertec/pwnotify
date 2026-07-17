"""Regressionstest für die Branding-Routen-Tenant-Scoping (Phase 3, Task 6 — Review-Fix).

Vor diesem Fix liefen `upload_logo`/`upload_favicon`/`delete_logo`/`delete_favicon` (und die
öffentlichen Lese-Routen) über `SettingsDep` -- die Owner-Session. `SettingsService._upsert`
stempelt `tenant_id` aus `current_tenant_or_none()` (siehe settings_service.py); auf der
Owner-Session liefert das `None`, und `Setting.tenant_id` ist NOT NULL + Teil des
Composite-PK -> jeder Logo-/Favicon-Upload oder -Löschvorgang endete mit `IntegrityError` ->
HTTP 500. Der Fix stellt branding.py wie settings.py auf `TenantSettingsDep`/`TenantSessionDep`
um (`get_tenant_session()` löst den Default-Tenant auf, auch ohne Auth).

Kein `TestClient`-Aufbau in dieser Suite (siehe test_route_tenant_scoping.py) -- der Beweis
läuft auf Handler-Ebene: die Routenfunktionen werden direkt mit einer über
`get_tenant_session()` aufgelösten Session getrieben, exakt der Pfad, den FastAPI beim echten
Request nimmt.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.api.deps import get_tenant_session
from app.api.routes import branding
from app.core.config import get_settings
from app.db import session as db_session
from app.services.settings_service import SettingsService
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers

# Harmloses SVG (wie in test_svg_upload_guard.py) -- braucht kein Pillow zum Rastern und
# _autotrim() lässt SVGs ohnehin unangetastet.
HARMLESS_SVG_LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <circle cx="5" cy="5" r="4" fill="#4F46E5"/>
</svg>"""


def _upload_file(data: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename="logo.svg",
        headers=Headers({"content-type": content_type}),
    )


@pytest_asyncio.fixture
async def tmp_data_dir(tmp_path) -> AsyncGenerator[None]:
    """Branding-Uploads landen unter `{data_dir}/branding` -- für den Test auf ein
    Wegwerfverzeichnis umbiegen, sonst würde unter dem produktiven `/data` geschrieben
    (existiert lokal/CI meist gar nicht). Gleiches ENV-Override-Pattern wie
    `migrated_engine` in conftest.py."""
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
async def real_default_tenant_id(migrated_engine: AsyncEngine) -> int:
    async with migrated_engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))).scalar_one()
        )


@pytest_asyncio.fixture
async def cleanup_branding_settings(migrated_engine: AsyncEngine) -> AsyncGenerator[None]:
    """Räumt die branding.*-Settings des Default-Tenants nach dem Test weg (residue-frei,
    Suite muss zweimal hintereinander grün laufen)."""
    yield
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM setting WHERE key IN ('branding.logo_path', 'branding.favicon_path')")
        )
        await conn.commit()


async def test_upload_and_delete_logo_via_tenant_session_no_500(
    tmp_data_dir: None,
    real_default_tenant_id: int,
    cleanup_branding_settings: None,
) -> None:
    """Der eigentliche Regressionstest: `upload_logo`/`delete_logo` über die tenant-gescopte
    Session (wie FastAPI sie via `TenantSettingsDep` injiziert) dürfen KEINEN
    `IntegrityError` werfen -- genau das war der Bug (Owner-Session -> tenant_id=None ->
    NOT-NULL-Verletzung -> HTTP 500)."""
    gen = get_tenant_session()
    session = await anext(gen)
    try:
        svc = SettingsService(session)
        file = _upload_file(HARMLESS_SVG_LOGO, "image/svg+xml")

        msg = await branding.upload_logo(None, svc, file)  # type: ignore[arg-type]
        assert msg.message

        stored_path = await svc.get("branding.logo_path")
        assert stored_path is not None, "Logo-Pfad wurde nicht gespeichert"

        row = (
            await session.execute(
                text("SELECT tenant_id FROM setting WHERE key = 'branding.logo_path'")
            )
        ).one()
        assert row.tenant_id == real_default_tenant_id, (
            f"Setting hängt am falschen Tenant: {row.tenant_id} != {real_default_tenant_id}"
        )

        del_msg = await branding.delete_logo(None, svc)  # type: ignore[arg-type]
        assert del_msg.message
        assert await svc.get("branding.logo_path") is None
    finally:
        await gen.aclose()


async def test_bare_owner_session_write_raises_integrity_error() -> None:
    """Dokumentiert den Vertrag, der den Bug ausgelöst hat: derselbe Setting-Write auf einer
    Owner-Session (kein aktiver Tenant-Kontext -- genau das, was `SettingsDep` VOR diesem Fix
    an branding.py reichte) muss mit einer NOT-NULL-Verletzung scheitern. Kein stiller
    Fallback, kein überraschendes HTTP 500 ohne erklärenden Fehler."""
    async for session in db_session.get_session():
        svc = SettingsService(session)
        with pytest.raises(IntegrityError):
            await svc.set("branding.logo_path", "/tmp/should-not-be-persisted.png")
        await session.rollback()
