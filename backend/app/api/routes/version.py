"""Update-Check: vergleicht die laufende Version mit dem neuesten GitHub-Release.

Fragt die öffentliche GitHub-API (keine Auth) an und cacht das Ergebnis prozesslokal,
um Rate-Limits/Latenz zu vermeiden. Fehlschläge sind unkritisch -> kein Update-Hinweis.
"""

from __future__ import annotations

import datetime as dt

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from ... import __version__
from ...core.logging import get_logger
from ..deps import CurrentUser, SettingsDep

router = APIRouter(prefix="/version", tags=["version"])
log = get_logger("version")

_LATEST_API = "https://api.github.com/repos/amslertec/pwnotify/releases/latest"
_RELEASE_PAGE = "https://github.com/amslertec/pwnotify/releases/latest"
_CACHE_TTL = dt.timedelta(hours=6)
_NOTES_MAX = 8000  # Release-Body defensiv begrenzen


class _Release(BaseModel):
    tag: str | None = None
    name: str | None = None
    notes: str | None = None
    url: str = _RELEASE_PAGE
    published_at: str | None = None


# Prozesslokaler Cache (best effort; pro Instanz).
_cached: _Release | None = None
_checked_at: dt.datetime | None = None


class VersionInfo(BaseModel):
    current: str
    latest: str | None = None
    update_available: bool = False
    release_url: str = _RELEASE_PAGE
    release_name: str | None = None
    notes: str | None = None
    published_at: str | None = None
    checked_at: dt.datetime | None = None
    enabled: bool = True


def _parse(v: str) -> tuple[int, ...]:
    """'v0.1.2' / '0.1.2-rc1' -> (0, 1, 2). Nicht-numerische Teile -> 0."""
    core = v.lstrip("vV").split("+")[0].split("-")[0]
    out: list[int] = []
    for part in core.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


async def _fetch_latest() -> _Release | None:
    global _cached, _checked_at
    now = dt.datetime.now(dt.UTC)
    if _checked_at is not None and now - _checked_at < _CACHE_TTL:
        return _cached
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_LATEST_API, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            data = resp.json()
            body = data.get("body") or None
            _cached = _Release(
                tag=data.get("tag_name"),
                name=data.get("name"),
                notes=body[:_NOTES_MAX] if body else None,
                url=data.get("html_url") or _RELEASE_PAGE,
                published_at=data.get("published_at"),
            )
        else:
            log.warning("update_check_status", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("update_check_failed", error=str(exc))
    # checked_at auch bei Fehler setzen, damit nicht bei jedem Request neu gefragt wird.
    _checked_at = now
    return _cached


@router.get("", response_model=VersionInfo)
async def version(_: CurrentUser, svc: SettingsDep) -> VersionInfo:
    settings = await svc.get_all()
    if not bool(settings.get("app.update_check", True)):
        return VersionInfo(current=__version__, enabled=False)
    rel = await _fetch_latest()
    tag = rel.tag if rel else None
    return VersionInfo(
        current=__version__,
        latest=tag,
        update_available=bool(tag and _is_newer(tag, __version__)),
        release_url=rel.url if rel else _RELEASE_PAGE,
        release_name=rel.name if rel else None,
        notes=rel.notes if rel else None,
        published_at=rel.published_at if rel else None,
        checked_at=_checked_at,
        enabled=True,
    )
