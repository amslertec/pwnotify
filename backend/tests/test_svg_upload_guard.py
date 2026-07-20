"""A8: SVG-Upload-Härtung von Denylist auf Allowlist-Parse.

SVG ist XML und darf Skripte, Event-Handler und externe Referenzen enthalten. Logo/Favicon
werden unter der eigenen Domain und ohne Authentifizierung ausgeliefert -- ein Skript darin
liefe im Origin der App. Die frühere Regex-Denylist war über Entity-/Zeichen-Kodierung, SMIL
(<set>/<animate>) und <use href="http…"> umgehbar. Ersetzt durch einen Allowlist-Parse:
Das SVG wird mit einem gehärteten XML-Parser (defusedxml, DTD/Entities/externe Refs aus)
gelesen und nur eine konservative Allowlist harmloser Logo-Elemente/-Attribute durchgelassen.
"""

from __future__ import annotations

import pytest
from app.api.routes.branding import _ASSET_HEADERS, _reject_active_svg
from app.core.errors import PwNotifyError

HARMLOSES_LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <circle cx="50" cy="50" r="40" fill="#4F46E5"/>
  <text x="50" y="55" text-anchor="middle" font-family="Inter">PW</text>
</svg>"""

# Ein echtes Logo mit internem Verlauf: fill=url(#id) + xlink:href="#id" sind zulässig.
HARMLOSES_GRADIENT_LOGO = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
    b'<defs><linearGradient id="g"><stop offset="0" stop-color="#000"/></linearGradient>'
    b'<linearGradient id="g2" xlink:href="#g"/></defs>'
    b'<rect x="0" y="0" width="100" height="100" fill="url(#g2)"/></svg>'
)


def test_harmless_logo_passes() -> None:
    _reject_active_svg(HARMLOSES_LOGO)


def test_harmless_gradient_logo_passes() -> None:
    _reject_active_svg(HARMLOSES_GRADIENT_LOGO)


@pytest.mark.parametrize(
    ("payload", "was"),
    [
        (b"<svg><script>alert(1)</script></svg>", "script-Element"),
        (b'<svg><circle onload="alert(1)"/></svg>', "Event-Handler"),
        (b'<svg><a xlink:href="javascript:alert(1)">x</a></svg>', "javascript:-URL + <a>"),
        (b"<svg><foreignObject><p>x</p></foreignObject></svg>", "foreignObject"),
        (b"<svg><iframe/></svg>", "iframe"),
        (b"<svg><SCRIPT>alert(1)</SCRIPT></svg>", "Grossschreibung"),
        # Bypass-Payloads, die die alte Denylist NICHT erkannte:
        (
            b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
            "XXE-Entity",
        ),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="x"/></svg>', "SMIL <set>"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x"/></svg>',
            "SMIL <animate>",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<use xlink:href="http://evil.tld/x"/></svg>',
            "<use> external",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<linearGradient xlink:href="http://evil.tld/x"/></svg>',
            "externe href-Referenz",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<linearGradient xlink:href="&#106;avascript:alert(1)"/></svg>',
            "entity-kodiertes javascript: in href",
        ),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<rect style="fill:url(http://evil.tld/x)"/></svg>',
            "externe url() im style",
        ),
    ],
)
def test_active_svg_is_rejected(payload: bytes, was: str) -> None:
    with pytest.raises(PwNotifyError) as err:
        _reject_active_svg(payload)
    assert err.value.code in ("svg_active_content", "svg_parse_failed"), was


def test_delivery_headers_neutralise_active_content() -> None:
    """Ausgelieferte Assets dürfen nicht als aktives Dokument interpretiert werden."""
    csp = _ASSET_HEADERS["Content-Security-Policy"]
    assert "sandbox" in csp
    assert "default-src 'none'" in csp
    assert _ASSET_HEADERS["X-Content-Type-Options"] == "nosniff"
