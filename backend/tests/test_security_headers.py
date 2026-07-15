"""Security-Header und CSP-Aufbau.

Die App lieferte gar keine Security-Header: sie liess sich in einen iframe einbetten
(Clickjacking auf Login und Admin-Aktionen) und hatte keinerlei Tiefenverteidigung gegen
XSS. Die CSP darf dabei nicht auf ``'unsafe-inline'`` ausweichen — sonst wäre sie gegen
genau den Fall wirkungslos, für den sie gedacht ist.
"""

from __future__ import annotations

from pathlib import Path

from app.core.security_headers import build_csp, inline_script_hashes

INDEX_MIT_INLINE = b"""<!doctype html>
<html><head>
<script>console.log("theme")</script>
<script src="/assets/index.js"></script>
<script>console.log("branding")</script>
</head><body></body></html>"""


def test_hashes_only_for_inline_scripts(tmp_path: Path) -> None:
    """Externe Skripte brauchen keinen Hash — nur die inline eingebetteten."""
    f = tmp_path / "index.html"
    f.write_bytes(INDEX_MIT_INLINE)
    hashes = inline_script_hashes(f)
    assert len(hashes) == 2
    assert all(h.startswith("'sha256-") and h.endswith("'") for h in hashes)


def test_hash_changes_with_script(tmp_path: Path) -> None:
    """Ändert sich ein Skript, muss der Hash mitwandern — sonst bräche die Seite still."""
    a = tmp_path / "a.html"
    a.write_bytes(b"<script>console.log(1)</script>")
    b = tmp_path / "b.html"
    b.write_bytes(b"<script>console.log(2)</script>")
    assert inline_script_hashes(a) != inline_script_hashes(b)


def test_missing_file_is_tolerated(tmp_path: Path) -> None:
    assert inline_script_hashes(tmp_path / "gibt-es-nicht.html") == []


def test_csp_has_no_unsafe_inline_for_scripts() -> None:
    csp = build_csp(["'sha256-abc'"])
    script_src = next(p for p in csp.split("; ") if p.startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    assert "'sha256-abc'" in script_src


def test_csp_blocks_framing_and_objects() -> None:
    csp = build_csp([])
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_csp_allows_what_the_app_needs() -> None:
    """QR-Codes (data:), Exporte (blob:) und lokale Fonts müssen weiter funktionieren."""
    csp = build_csp([])
    img = next(p for p in csp.split("; ") if p.startswith("img-src"))
    assert "data:" in img and "blob:" in img
    assert "font-src 'self' data:" in csp
