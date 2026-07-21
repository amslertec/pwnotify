"""Lazy Graph-Profilfoto pro Entra-User -- tenant-gescoped auf Platte gecacht.

Sicherheitskritisch (zwei tragende Kontrollen):

1. **Path-Traversal**: `entra_id` kommt aus dem URL-Pfad und wird zu einem Dateipfad. Er
   wird VOR jedem Dateisystemzugriff gegen `_ENTRA_ID_RE` (`^[A-Za-z0-9-]{1,64}$`, das
   Format eines Entra-GUID) validiert -- alles andere -> 404, ohne je ein Verzeichnis
   anzulegen oder Graph zu kontaktieren.
2. **Aktiver-Mandant-Scoping**: die Graph-Config kommt vom AKTIVEN Mandanten (der Aufrufer
   in `routes/entra_avatar.py` reicht `settings` aus `TenantSettingsDep` durch -- NIE ein
   ungescoptes `SettingsService.get_all()`), und der Cache liegt unter `{tenant_id}/`.

Ein Foto wird lazy geholt und gecacht; ein Graph-Fehler oder "kein Foto" führt NIE zu 500,
sondern zu 404 (das Frontend fällt dann auf Initialen zurück) plus einem `.none`-Sentinel,
damit nicht jeder Render Graph erneut belastet.
"""

from __future__ import annotations

import contextlib
import io
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, Response

from ..core.config import get_settings
from ..core.errors import NotFoundError
from .graph.client import GraphClient, GraphConfig

# Entra-Objekt-IDs sind GUIDs (Hex + Bindestriche). Bewusst restriktiv: kein `.`, kein `/`,
# kein Whitespace -- damit ist Path-Traversal (`..`, `a/b`) strukturell ausgeschlossen.
_ENTRA_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

PHOTO_TTL = timedelta(days=7)
# "No photo" is cached for a full week (matching PHOTO_TTL): profile photos are rarely added
# after the fact, and a 1-day negative TTL meant a large photoless tenant re-hit Graph with a
# 404 for every such user every day. A week cuts that Graph traffic ~7x with negligible staleness.
NEG_TTL = timedelta(days=7)

# Browser-Cache: verhindert, dass jeder Tabellen-Render die Route erneut trifft.
_BROWSER_MAX_AGE = 3600


def _avatars_root() -> Path:
    """Wurzel des Foto-Caches im `data_dir` (mirror von `routes/auth.py:_avatar_dir`)."""
    return Path(get_settings().data_dir) / "entra-avatars"


# M9: without a cap, Pillow decodes whatever pixel area a file declares -- a tiny file
# claiming a huge width/height forces a huge in-memory bitmap allocation (decompression
# bomb). 24 MP comfortably covers any legitimate avatar photo while still catching bombs;
# Pillow raises `Image.DecompressionBombError` (a plain `Exception`) once decoded pixels
# exceed 2x this value, which the `except Exception` below already turns into a clean `None`.
_MAX_IMAGE_PIXELS = 24_000_000


def _process_avatar(data: bytes) -> bytes | None:
    """Bild zentriert quadratisch zuschneiden -> 256x256 PNG. None bei kaputtem Bild.

    Selbstständige Replik von `routes/auth.py:_process_avatar` -- bewusst NICHT importiert,
    um keine Import-Abhängigkeit auf das Auth-Route-Modul (Zirkular-Risiko) zu schaffen.
    """
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

        img = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize(
            (256, 256), Image.Resampling.LANCZOS
        )
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # ungültige/kaputte Bilddatei
        return None


async def _fetch_from_graph(entra_id: str, settings: dict[str, Any]) -> bytes | None:
    """Foto über den GraphClient des AKTIVEN Mandanten holen. Jeder Fehler -> None.

    Config exakt wie `services/graph/sync.py` -- ausschliesslich aus den durchgereichten,
    bereits tenant-gescopten `settings`. Kein 500 nach aussen: Graph-/Transportfehler werden
    hier zu None und vom Aufrufer als 404 + Negative-Cache behandelt.
    """
    try:
        client = GraphClient(
            GraphConfig(
                tenant_id=str(settings.get("graph.tenant_id") or ""),
                client_id=str(settings.get("graph.client_id") or ""),
                client_secret=str(settings.get("graph.client_secret") or ""),
                cloud=str(settings.get("graph.cloud") or "global"),
            )
        )
        return await client.get_user_photo(entra_id)
    except Exception:
        return None


def _fresh(path: Path, ttl: timedelta) -> bool:
    """True, wenn `path` existiert und jünger als `ttl` ist (Vergleich gegen mtime). Jeder
    `OSError` (fehlende Datei ODER nicht erreichbares `data_dir`, z. B. read-only `/data` vor
    Volume-Mount) -> nicht frisch, statt die Anfrage mit 500 zu quittieren."""
    try:
        return time.time() - path.stat().st_mtime < ttl.total_seconds()
    except OSError:
        return False


def _serve_file(path: Path) -> FileResponse:
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": f"max-age={_BROWSER_MAX_AGE}"},
    )


def _no_photo() -> NotFoundError:
    return NotFoundError("Kein Profilfoto vorhanden.", code="no_photo")


async def serve(entra_id: str, tenant_id: int | None, settings: dict[str, Any]) -> Response:
    """Gecachtes Profilfoto ausliefern oder lazy holen. 404 statt 500 bei jedem Fehlerfall.

    `tenant_id` ist der aktive Mandant (int); None (kein aktiver Mandant) -> 404. `entra_id`
    wird VOR jedem Dateisystemzugriff validiert (Path-Traversal-Schutz).
    """
    # Kontrolle 1+2: kein aktiver Mandant, oder unerlaubtes id-Format -> sofort 404, bevor
    # irgendein Pfad gebaut oder Verzeichnis angelegt wird.
    if tenant_id is None or _ENTRA_ID_RE.fullmatch(entra_id) is None:
        raise _no_photo()

    tenant_dir = _avatars_root() / str(tenant_id)
    png = tenant_dir / f"{entra_id}.png"
    none = tenant_dir / f"{entra_id}.none"

    if _fresh(png, PHOTO_TTL):
        return _serve_file(png)
    if _fresh(none, NEG_TTL):
        raise _no_photo()

    raw = await _fetch_from_graph(entra_id, settings)
    processed = _process_avatar(raw) if raw else None
    if processed is None:
        # Negative-Cache best-effort: ist `data_dir` nicht schreibbar, trotzdem sauber 404.
        with contextlib.suppress(OSError):
            tenant_dir.mkdir(parents=True, exist_ok=True)
            none.write_bytes(b"")
        raise _no_photo()

    # Foto cachen ist best-effort: schlägt der Schreibzugriff fehl (read-only/nicht erreichbares
    # `data_dir`), liefern wir die Bytes direkt aus dem Speicher statt zu 500en.
    try:
        tenant_dir.mkdir(parents=True, exist_ok=True)
        png.write_bytes(processed)
        with contextlib.suppress(OSError):  # ein vorheriger Negativ-Eintrag ist veraltet
            none.unlink()
        return _serve_file(png)
    except OSError:
        return Response(
            content=processed,
            media_type="image/png",
            headers={"Cache-Control": f"max-age={_BROWSER_MAX_AGE}"},
        )
