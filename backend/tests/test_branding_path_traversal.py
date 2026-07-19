"""C2: path traversal via branding path settings must not exfiltrate the Fernet key.

Three layers, all required:
(a) the branding.*_path validator rejects paths outside the branding directory (400);
(b) GET /branding/logo|favicon only serve a file contained in the branding dir (else 404);
(c) the mail logo attachment ignores a stored path outside the branding dir.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from app.api.deps import get_public_tenant_session
from app.api.routes import branding
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.services import templating
from app.services.settings_service import SettingsService
from app.services.settings_validators import branding_dir, branding_path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def tmp_data_dir(tmp_path) -> AsyncGenerator[Path]:
    prev = os.environ.get("PWNOTIFY_DATA_DIR")
    os.environ["PWNOTIFY_DATA_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    if prev is None:
        os.environ.pop("PWNOTIFY_DATA_DIR", None)
    else:
        os.environ["PWNOTIFY_DATA_DIR"] = prev
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def cleanup_branding_settings(migrated_engine: AsyncEngine) -> AsyncGenerator[None]:
    yield
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM setting WHERE key IN ('branding.logo_path', 'branding.favicon_path')")
        )
        await conn.commit()


# --- (a) validator -------------------------------------------------------------- #
def test_validator_rejects_relative_traversal(tmp_data_dir: Path) -> None:
    with pytest.raises(ValidationError) as ei:
        branding_path("../../secret.key")
    assert ei.value.status_code == 400


def test_validator_rejects_absolute_outside_path(tmp_data_dir: Path) -> None:
    with pytest.raises(ValidationError):
        branding_path(str(tmp_data_dir / "secret.key"))


def test_validator_accepts_path_inside_branding_dir(tmp_data_dir: Path) -> None:
    inside = str(branding_dir() / "logo.png")
    assert branding_path(inside) == inside


def test_validator_allows_clearing(tmp_data_dir: Path) -> None:
    assert branding_path(None) is None


# --- (b) get_logo containment --------------------------------------------------- #
async def test_get_logo_refuses_escaped_stored_path(
    tmp_data_dir: Path, cleanup_branding_settings: None
) -> None:
    # A secret file OUTSIDE the branding dir (mirrors /data/secret.key).
    secret = tmp_data_dir / "secret.key"
    secret.write_bytes(b"FERNET-KEY-BYTES")

    gen = get_public_tenant_session()
    session = await anext(gen)
    try:
        svc = SettingsService(session)
        # Seed a malicious stored value directly (bypassing the validator) to simulate a
        # pre-existing / tampered row.
        await svc._upsert("branding.logo_path", str(secret), False)
        await session.commit()
        with pytest.raises(NotFoundError):
            await branding.get_logo(svc)
    finally:
        await gen.aclose()


# --- (c) mail attachment containment -------------------------------------------- #
def test_mail_attachment_ignores_escaped_path(tmp_data_dir: Path) -> None:
    secret = tmp_data_dir / "secret.key"
    secret.write_bytes(b"FERNET-KEY-BYTES")
    settings = {"branding.logo_path": str(secret)}
    # static_dir without a default logo -> nothing legitimate to attach.
    result = templating.build_logo_attachments(settings, str(tmp_data_dir / "static"))
    assert result == []


def test_mail_attachment_serves_contained_logo(tmp_data_dir: Path) -> None:
    bdir = branding_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    logo = bdir / "logo.png"
    logo.write_bytes(b"PNGDATA")
    settings = {"branding.logo_path": str(logo)}
    result = templating.build_logo_attachments(settings, str(tmp_data_dir / "static"))
    assert len(result) == 1
    assert result[0][1] == b"PNGDATA"
