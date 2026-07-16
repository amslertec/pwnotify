"""Bildformat am Inhalt erkennen, nicht am Wort des Clients.

Der ``Content-Type`` eines Uploads kommt vom Client und ist damit frei erfunden. Beim
Branding-Upload wird bei einem Pillow-Fehler bewusst das Original behalten (SVG lässt
sich nicht rastern) — ohne Signaturprüfung landen so beliebige Bytes unter ``.png`` im
Auslieferungsverzeichnis. Zusammen mit einem fehlenden ``nosniff`` wäre das ein Weg,
den Browser zu einer falschen Interpretation zu verleiten.
"""

from __future__ import annotations

import re

# Signaturen am Dateianfang. Bewusst knapp gehalten: erkannt werden muss nur, was auch
# erlaubt ist — alles andere fliegt ohnehin raus.
_SIGNATUREN: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)

# SVG ist Text: Vor dem <svg> dürfen BOM, XML-Deklaration, DOCTYPE oder Kommentare stehen.
_SVG = re.compile(rb"^\s*(<\?xml[^>]*\?>\s*|<!--.*?-->\s*|<!DOCTYPE[^>]*>\s*)*<svg\b", re.S | re.I)


def sniff(data: bytes) -> str | None:
    """Erkennt den tatsächlichen Typ. ``None``, wenn es keins der erlaubten Formate ist."""
    if not data:
        return None
    for magic, mime in _SIGNATUREN:
        if data.startswith(magic):
            return mime
    # WebP: "RIFF" + 4 Byte Länge + "WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _SVG.match(data.lstrip(b"\xef\xbb\xbf")[:2048]):
        return "image/svg+xml"
    return None


def matches(data: bytes, declared: str) -> bool:
    """Passt der Inhalt zu dem, was der Client behauptet?

    ICO wird unter zwei MIME-Typen geführt; beide meinen dasselbe Format.
    """
    tatsaechlich = sniff(data)
    if tatsaechlich is None:
        return False
    if declared in ("image/x-icon", "image/vnd.microsoft.icon"):
        return tatsaechlich == "image/x-icon"
    return tatsaechlich == declared
