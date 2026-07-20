"""A8: SVG upload hardening from denylist to allowlist parse.

SVG is XML and may contain scripts, event handlers, and external references. Logo/favicon
are served under the app's own domain and without authentication -- a script inside would
run in the app's origin. The former regex denylist was bypassable via entity/character
encoding, SMIL (<set>/<animate>), and <use href="http…">. Replaced by an allowlist parse:
the SVG is read with a hardened XML parser (defusedxml, DTD/entities/external refs off)
and only a conservative allowlist of harmless logo elements/attributes is let through.
"""

from __future__ import annotations

import pytest
from app.api.routes.branding import _ASSET_HEADERS, _reject_active_svg
from app.core.errors import PwNotifyError

HARMLOSES_LOGO = b"""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <circle cx="50" cy="50" r="40" fill="#4F46E5"/>
  <text x="50" y="55" text-anchor="middle" font-family="Inter">PW</text>
</svg>"""

# A real logo with an internal gradient: fill=url(#id) + xlink:href="#id" are allowed.
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
        # Bypass payloads that the old denylist did NOT catch:
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
    """Delivered assets must not be interpretable as an active document."""
    csp = _ASSET_HEADERS["Content-Security-Policy"]
    assert "sandbox" in csp
    assert "default-src 'none'" in csp
    assert _ASSET_HEADERS["X-Content-Type-Options"] == "nosniff"
