"""Branding: öffentliches Theming + Logo/Favicon-Upload und -Auslieferung."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...core import imagetype
from ...core.config import get_settings
from ...core.errors import NotFoundError, PwNotifyError
from ...db.tenant_context import current_tenant_or_none
from ...schemas.common import Message
from ...services import audit
from ...services.settings_validators import contained_path
from ..deps import AdminUser, PublicTenantSettingsDep, TenantSettingsDep, TenantWriteSessionDep

router = APIRouter(prefix="/branding", tags=["branding"])

_ALLOWED = {
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
_MAX_BYTES = 2 * 1024 * 1024

# SVG ist XML und darf Skripte, Event-Handler und externe Referenzen enthalten. Da die
# Datei unter der eigenen Domain ausgeliefert wird, liefe solcher Code im App-Origin.
# Statt zu säubern (Sanitizer sind notorisch löchrig) wird abgelehnt: ein Logo braucht
# nichts davon.
_SVG_ACTIVE_PATTERNS = (
    (re.compile(rb"<\s*script", re.I), "Skript-Element"),
    (re.compile(rb"\son[a-z]+\s*=", re.I), "Event-Handler-Attribut"),
    (re.compile(rb"javascript\s*:", re.I), "javascript:-URL"),
    (re.compile(rb"<\s*foreignObject", re.I), "foreignObject-Element"),
    (re.compile(rb"<\s*!ENTITY", re.I), "XML-Entity (XXE)"),
    (re.compile(rb"<\s*iframe", re.I), "iframe-Element"),
)

# Verhindert, dass ein Bild als aktives Dokument interpretiert wird. `sandbox` entzieht
# der Antwort u. a. Skriptausführung und den eigenen Origin; als <img> eingebunden —
# so nutzt das Frontend die Route — bleibt alles unverändert nutzbar.
_ASSET_HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _reject_active_svg(data: bytes) -> None:
    """Lehnt SVGs mit aktiven Inhalten ab (Stored XSS im eigenen Origin)."""
    for pattern, label in _SVG_ACTIVE_PATTERNS:
        if pattern.search(data):
            raise PwNotifyError(
                f"Das SVG enthält aktive Inhalte ({label}) und wurde abgelehnt. "
                "Bitte ein Logo ohne Skripte hochladen — oder PNG/WebP verwenden.",
                code="svg_active_content",
            )


def _branding_root() -> Path:
    """Shared root that holds every tenant's branding assets (plus legacy, pre-tenant-scoping
    uploads directly at its top level -- see `_tenant_branding_dir`)."""
    d = Path(get_settings().data_dir) / "branding"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tenant_branding_dir() -> Path:
    """Per-tenant storage subdirectory for NEW uploads (M5 fix).

    `_branding_root()` used to be written into directly with a fixed stem
    (`"logo"`/`"favicon"`), shared by every tenant -- tenant A's upload silently
    overwrote/deleted tenant B's file on disk. Scoping the write path by the active tenant
    (from the ContextVar bound by `tenant_scoped_session`, set for the duration of every
    branding route via `TenantSettingsDep`) isolates uploads without touching read
    containment, which still resolves against the shared root (see `_safe_branding_file`).
    """
    tid = current_tenant_or_none()
    if tid is None:
        raise PwNotifyError("Kein Mandantenkontext.", code="tenant_required")
    d = _branding_root() / str(tid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_branding_file(path: str | None) -> Path | None:
    """Resolved branding file if it exists inside the branding root, else None.

    Deliberately checked against the shared ROOT, not the per-tenant subdirectory: existing
    `branding.*_path` values still point at the legacy flat layout
    (`{data}/branding/logo.png`), and this must keep resolving them. New uploads land
    tenant-isolated under `_tenant_branding_dir()`, which is itself inside the root, so it
    still passes containment.
    """
    if not path:
        return None
    real = contained_path(_branding_root().resolve(), path)
    if real is None or not real.is_file():
        return None
    return real


@router.get("")
async def public_branding(svc: PublicTenantSettingsDep, response: Response) -> dict[str, Any]:
    """Öffentlich (kein Auth) — für Login-/Setup-Seiten-Theming."""
    response.headers["Cache-Control"] = "no-store"
    s = await svc.get_all()
    return {
        "app_name": s.get("branding.app_name") or "PwNotify",
        "company_name": s.get("branding.company_name") or "",
        "primary_color": s.get("branding.primary_color") or "#4F46E5",
        "reset_url": s.get("branding.reset_url") or "",
        "has_logo": bool(s.get("branding.logo_path")),
        "has_favicon": bool(s.get("branding.favicon_path")),
        # Datei-Änderungszeit als Cache-Buster -> neues Bild erscheint sofort.
        "logo_version": _file_version(s.get("branding.logo_path")),
        "favicon_version": _file_version(s.get("branding.favicon_path")),
    }


def _file_version(path: str | None) -> int:
    """Cache-buster derived from mtime — must go through the same containment guard as
    get_logo/get_favicon. Without it, this (unauthenticated) helper would leak an
    existence/mtime oracle for arbitrary paths stored in branding.*_path.
    """
    real = _safe_branding_file(path)
    if real is None:
        return 0
    return int(real.stat().st_mtime)


# Zielhöhe für gespeicherte Raster-Logos. Anzeige max. ~56px (Login) -> bei 3x-HiDPI
# ~168px physisch; 192px hält alle Displays gestochen scharf bei kleiner Dateigrösse.
_LOGO_TARGET_HEIGHT = 192

# M9: without a cap, Pillow decodes whatever pixel area a file declares -- a tiny file
# claiming a huge width/height forces a huge in-memory bitmap allocation (decompression
# bomb). 24 MP comfortably covers any legitimate logo while still catching bombs; Pillow
# raises `Image.DecompressionBombError` (a plain `Exception`) once decoded pixels exceed
# 2x this value, which the `except Exception` below already turns into a clean `None`.
_MAX_IMAGE_PIXELS = 24_000_000


def _autotrim(data: bytes) -> bytes | None:
    """Raster-Logo aufbereiten: transparente Ränder abschneiden, auf HiDPI-Höhe
    normalisieren (Lanczos) und als optimiertes PNG zurückgeben.

    Gibt None zurück, wenn nicht verarbeitbar (z. B. SVG) -> Original wird behalten.
    """
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

        img = Image.open(io.BytesIO(data)).convert("RGBA")
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
        # Auf einheitliche Höhe skalieren (hoch- oder runterskalieren) für gleichmässige
        # Schärfe auf HiDPI-Displays. Lanczos = hochwertigste Resampling-Methode.
        w, h = img.size
        if h > 0 and w > 0 and h != _LOGO_TARGET_HEIGHT:
            new_w = max(1, round(w * _LOGO_TARGET_HEIGHT / h))
            img = img.resize((new_w, _LOGO_TARGET_HEIGHT), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # bei Fehler unverändert speichern
        return None


async def _save_upload(file: UploadFile, stem: str, *, trim: bool = False) -> str:
    ext = _ALLOWED.get(file.content_type or "")
    if ext is None:
        raise PwNotifyError(
            "Nicht unterstütztes Format (PNG, SVG, WebP, JPG, ICO).", code="unsupported_format"
        )
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise PwNotifyError("Datei zu gross (max. 2 MB).", code="file_too_large")
    # Der Content-Type kommt vom Client — der Inhalt muss dazu passen. Sonst landete
    # bei einem Pillow-Fehler (unten wird dann das Original behalten) beliebiges
    # Material unter einer Bild-Endung im Auslieferungsverzeichnis.
    if not imagetype.matches(data, file.content_type or ""):
        raise PwNotifyError(
            "Der Dateiinhalt passt nicht zum angegebenen Format.", code="content_type_mismatch"
        )
    if ext == ".svg":
        _reject_active_svg(data)
    # Logo: transparente Ränder automatisch abschneiden (SVG bleibt unverändert).
    if trim and ext != ".svg":
        trimmed = _autotrim(data)
        if trimmed is not None:
            data = trimmed
            ext = ".png"
    # Remove previous variants so only one file remains -- scoped to this tenant's own
    # subdirectory only (no more cross-tenant deletion, M5).
    tenant_dir = _tenant_branding_dir()
    for old in tenant_dir.glob(f"{stem}.*"):
        old.unlink(missing_ok=True)
    target = tenant_dir / f"{stem}{ext}"
    target.write_bytes(data)
    return str(target)


async def _audit_branding_change(
    session: AsyncSession, *, admin: AdminUser, request: Request, asset: str, op: str
) -> None:
    """Shared audit write for the four upload/delete routes below (Security Phase 5, Task
    8/M10). Runs on its OWN `TenantWriteSessionDep` connection, separate from `svc`'s --
    `svc: TenantSettingsDep` already committed its own change (`SettingsService.set`); this
    just records + commits the audit entry on the write-gated session."""
    await audit.record(
        session,
        action=audit.BRANDING_CHANGED,
        actor=admin,
        request=request,
        detail={"asset": asset, "op": op},
    )
    await session.commit()


@router.post("/logo", response_model=Message)
async def upload_logo(
    request: Request,
    admin: AdminUser,
    svc: TenantSettingsDep,
    session: TenantWriteSessionDep,
    file: UploadFile = File(...),
) -> Message:
    path = await _save_upload(file, "logo", trim=True)
    await svc.set("branding.logo_path", path)
    await _audit_branding_change(session, admin=admin, request=request, asset="logo", op="upload")
    return Message(message="Logo gespeichert.")


@router.post("/favicon", response_model=Message)
async def upload_favicon(
    request: Request,
    admin: AdminUser,
    svc: TenantSettingsDep,
    session: TenantWriteSessionDep,
    file: UploadFile = File(...),
) -> Message:
    path = await _save_upload(file, "favicon")
    await svc.set("branding.favicon_path", path)
    await _audit_branding_change(
        session, admin=admin, request=request, asset="favicon", op="upload"
    )
    return Message(message="Favicon gespeichert.")


async def _clear_upload(svc: TenantSettingsDep, key: str, stem: str) -> None:
    for old in _tenant_branding_dir().glob(f"{stem}.*"):
        old.unlink(missing_ok=True)
    await svc.set(key, None)


@router.delete("/logo", response_model=Message)
async def delete_logo(
    request: Request, admin: AdminUser, svc: TenantSettingsDep, session: TenantWriteSessionDep
) -> Message:
    await _clear_upload(svc, "branding.logo_path", "logo")
    await _audit_branding_change(session, admin=admin, request=request, asset="logo", op="delete")
    return Message(message="Logo entfernt — Standard aktiv.")


@router.delete("/favicon", response_model=Message)
async def delete_favicon(
    request: Request, admin: AdminUser, svc: TenantSettingsDep, session: TenantWriteSessionDep
) -> Message:
    await _clear_upload(svc, "branding.favicon_path", "favicon")
    await _audit_branding_change(
        session, admin=admin, request=request, asset="favicon", op="delete"
    )
    return Message(message="Favicon entfernt — Standard aktiv.")


@router.get("/logo")
async def get_logo(svc: PublicTenantSettingsDep) -> Response:
    real = _safe_branding_file(await svc.get("branding.logo_path"))
    if real is None:
        raise NotFoundError("Kein Logo gesetzt.", code="no_logo")
    return FileResponse(real, headers=_ASSET_HEADERS)


@router.get("/favicon")
async def get_favicon(svc: PublicTenantSettingsDep) -> Response:
    real = _safe_branding_file(await svc.get("branding.favicon_path"))
    if real is None:
        raise NotFoundError("Kein Favicon gesetzt.", code="no_favicon")
    return FileResponse(real, headers=_ASSET_HEADERS)
