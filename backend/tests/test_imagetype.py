"""Bildformat wird am Inhalt erkannt, nicht am Wort des Clients.

Der Content-Type eines Uploads ist frei erfunden. Beim Branding wird bei einem
Pillow-Fehler bewusst das Original behalten (SVG lässt sich nicht rastern) — ohne
Signaturprüfung landeten so beliebige Bytes unter einer Bild-Endung im
Auslieferungsverzeichnis.
"""

from __future__ import annotations

import pytest
from app.core.imagetype import matches, sniff

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16
ICO = b"\x00\x00\x01\x00" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'


@pytest.mark.parametrize(
    ("daten", "erwartet"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
        (ICO, "image/x-icon"),
        (SVG, "image/svg+xml"),
        (b"GIF89a" + b"\x00" * 16, "image/gif"),
    ],
)
def test_sniff_erkennt_formate(daten: bytes, erwartet: str) -> None:
    assert sniff(daten) == erwartet


def test_svg_mit_xml_deklaration() -> None:
    assert sniff(b'<?xml version="1.0"?>\n<svg xmlns="x"></svg>') == "image/svg+xml"


def test_svg_mit_bom_und_kommentar() -> None:
    assert sniff(b"\xef\xbb\xbf<!-- Logo --><svg></svg>") == "image/svg+xml"


def test_fremde_inhalte_werden_nicht_erkannt() -> None:
    assert sniff(b"#!/bin/sh\nrm -rf /") is None
    assert sniff(b"MZ\x90\x00") is None  # Windows-Executable
    assert sniff(b"") is None


def test_passende_kombination() -> None:
    assert matches(PNG, "image/png")
    assert matches(SVG, "image/svg+xml")


def test_luege_wird_erkannt() -> None:
    """Der Kernfall: Skript als PNG deklariert."""
    assert not matches(b"#!/bin/sh\nrm -rf /", "image/png")
    assert not matches(JPEG, "image/png")


def test_ico_unter_beiden_mime_typen() -> None:
    assert matches(ICO, "image/x-icon")
    assert matches(ICO, "image/vnd.microsoft.icon")
