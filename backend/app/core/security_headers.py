"""Security-Header für alle Antworten (Clickjacking, XSS-Tiefenverteidigung, HSTS).

Die CSP muss ohne ``'unsafe-inline'`` auskommen, sonst wäre sie gegen XSS wertlos.
``index.html`` enthält aber zwei bewusst inline gehaltene Skripte (Theme vor dem ersten
Rendern, Branding-Vorabruf) — beide müssen laufen, bevor React geladen ist. Statt sie
freizugeben, werden ihre SHA-256-Hashes beim Start aus der ausgelieferten Datei gelesen
und in die Richtlinie aufgenommen. Ändert jemand die Skripte, ändert sich der Hash
automatisch mit; es gibt keine manuell gepflegte Liste, die veralten könnte.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging import get_logger

log = get_logger("security_headers")

_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", re.S | re.I)


def inline_script_hashes(index_html: Path) -> list[str]:
    """CSP-Hashes der Inline-Skripte aus index.html (leer, wenn nicht lesbar)."""
    try:
        html = index_html.read_bytes()
    except OSError:
        return []
    hashes = []
    for body in _INLINE_SCRIPT.findall(html):
        digest = hashlib.sha256(body).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


def build_csp(script_hashes: list[str]) -> str:
    """Content-Security-Policy zusammensetzen.

    ``style-src`` erlaubt ``'unsafe-inline'``: React setzt Styles über das
    style-Attribut (``style={{…}}``), das sonst blockiert würde. CSS-Injection ist
    ungleich weniger gefährlich als Skriptausführung — bei ``script-src`` bleibt es
    deshalb bei Hashes.
    """
    script_src = " ".join(["'self'", *script_hashes])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self' 'unsafe-inline'",
            # data: für QR-Codes (2FA-Einrichtung) und Icons, blob: für CSV/XLSX-Downloads
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "form-action 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            # Clickjacking: die App darf nirgends eingebettet werden
            "frame-ancestors 'none'",
        ]
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Setzt die Header auf jeder Antwort, ohne bestehende zu überschreiben.

    Einzelne Routen (z. B. die Branding-Assets) liefern bewusst eine eigene, strengere
    Richtlinie mit — die bleibt erhalten.
    """

    def __init__(self, app: object, *, csp: str, hsts: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._csp = csp
        self._hsts = hsts

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", self._csp)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self._hsts:
            # Nur bei HTTPS-Betrieb — über HTTP wäre der Header wirkungslos und würde
            # eine reine LAN-Installation bei einem späteren Wechsel aussperren.
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
