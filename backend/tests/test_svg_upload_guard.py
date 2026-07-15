"""Schutz gegen Stored XSS über hochgeladene SVG-Logos.

SVG ist XML und darf Skripte enthalten. Logo und Favicon werden unter der eigenen Domain
und ohne Authentifizierung ausgeliefert (`/api/branding/favicon`) — ein Skript darin liefe
also im Origin der App und könnte im Namen angemeldeter Benutzer handeln. Reproduziert:
ein SVG mit <script> wurde unverändert gespeichert und als `image/svg+xml` ausgeliefert.
"""

from __future__ import annotations

import pytest
from app.api.routes.branding import _ASSET_HEADERS, _reject_active_svg
from app.core.errors import PwNotifyError

HARMLOSES_LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <circle cx="50" cy="50" r="40" fill="#4F46E5"/>
  <text x="50" y="55" text-anchor="middle" font-family="Inter">PW</text>
</svg>"""


def test_harmless_logo_passes() -> None:
    """Ein normales Logo-SVG muss weiterhin funktionieren."""
    _reject_active_svg(HARMLOSES_LOGO)


@pytest.mark.parametrize(
    ("payload", "was"),
    [
        (b"<svg><script>alert(1)</script></svg>", "script-Element"),
        (b'<svg><circle onload="alert(1)"/></svg>', "Event-Handler"),
        (b'<svg><a xlink:href="javascript:alert(1)">x</a></svg>', "javascript:-URL"),
        (b"<svg><foreignObject><body>x</body></foreignObject></svg>", "foreignObject"),
        (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg/>', "XXE"),
        (b'<svg><iframe src="//evil"/></svg>', "iframe"),
        (b"<svg><SCRIPT>alert(1)</SCRIPT></svg>", "Grossschreibung"),
        (b'<svg><circle ONLOAD ="alert(1)"/></svg>', "Leerzeichen vor ="),
    ],
)
def test_active_svg_is_rejected(payload: bytes, was: str) -> None:
    with pytest.raises(PwNotifyError) as err:
        _reject_active_svg(payload)
    assert err.value.code == "svg_active_content", was


def test_delivery_headers_neutralise_active_content() -> None:
    """Ausgelieferte Assets dürfen nicht als aktives Dokument interpretiert werden."""
    csp = _ASSET_HEADERS["Content-Security-Policy"]
    assert "sandbox" in csp
    assert "default-src 'none'" in csp
    assert _ASSET_HEADERS["X-Content-Type-Options"] == "nosniff"
