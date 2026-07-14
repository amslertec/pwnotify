"""Branding: öffentliches Theming + Logo/Favicon-Upload und -Auslieferung."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, Response

from ...core.config import get_settings
from ...core.errors import NotFoundError, PwNotifyError
from ...schemas.common import Message
from ..deps import CurrentUser, SettingsDep

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


def _branding_dir() -> Path:
    d = Path(get_settings().data_dir) / "branding"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("")
async def public_branding(svc: SettingsDep, response: Response) -> dict[str, Any]:
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
    if path and Path(path).exists():
        return int(Path(path).stat().st_mtime)
    return 0


# Zielhöhe für gespeicherte Raster-Logos. Anzeige max. ~56px (Login) -> bei 3x-HiDPI
# ~168px physisch; 192px hält alle Displays gestochen scharf bei kleiner Dateigrösse.
_LOGO_TARGET_HEIGHT = 192


def _autotrim(data: bytes) -> bytes | None:
    """Raster-Logo aufbereiten: transparente Ränder abschneiden, auf HiDPI-Höhe
    normalisieren (Lanczos) und als optimiertes PNG zurückgeben.

    Gibt None zurück, wenn nicht verarbeitbar (z. B. SVG) -> Original wird behalten.
    """
    try:
        from PIL import Image

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
    # Logo: transparente Ränder automatisch abschneiden (SVG bleibt unverändert).
    if trim and ext != ".svg":
        trimmed = _autotrim(data)
        if trimmed is not None:
            data = trimmed
            ext = ".png"
    # alte Varianten entfernen, damit nur eine Datei existiert
    for old in _branding_dir().glob(f"{stem}.*"):
        old.unlink(missing_ok=True)
    target = _branding_dir() / f"{stem}{ext}"
    target.write_bytes(data)
    return str(target)


@router.post("/logo", response_model=Message)
async def upload_logo(_: CurrentUser, svc: SettingsDep, file: UploadFile = File(...)) -> Message:
    path = await _save_upload(file, "logo", trim=True)
    await svc.set("branding.logo_path", path)
    return Message(message="Logo gespeichert.")


@router.post("/favicon", response_model=Message)
async def upload_favicon(_: CurrentUser, svc: SettingsDep, file: UploadFile = File(...)) -> Message:
    path = await _save_upload(file, "favicon")
    await svc.set("branding.favicon_path", path)
    return Message(message="Favicon gespeichert.")


async def _clear_upload(svc: SettingsDep, key: str, stem: str) -> None:
    for old in _branding_dir().glob(f"{stem}.*"):
        old.unlink(missing_ok=True)
    await svc.set(key, None)


@router.delete("/logo", response_model=Message)
async def delete_logo(_: CurrentUser, svc: SettingsDep) -> Message:
    await _clear_upload(svc, "branding.logo_path", "logo")
    return Message(message="Logo entfernt — Standard aktiv.")


@router.delete("/favicon", response_model=Message)
async def delete_favicon(_: CurrentUser, svc: SettingsDep) -> Message:
    await _clear_upload(svc, "branding.favicon_path", "favicon")
    return Message(message="Favicon entfernt — Standard aktiv.")


@router.get("/logo")
async def get_logo(svc: SettingsDep) -> Response:
    path = await svc.get("branding.logo_path")
    if not path or not Path(path).exists():
        raise NotFoundError("Kein Logo gesetzt.", code="no_logo")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/favicon")
async def get_favicon(svc: SettingsDep) -> Response:
    path = await svc.get("branding.favicon_path")
    if not path or not Path(path).exists():
        raise NotFoundError("Kein Favicon gesetzt.", code="no_favicon")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})
