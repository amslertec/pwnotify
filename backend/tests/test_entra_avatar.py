"""Angriffsorientierte Tests für das lazy Graph-Profilfoto (Task C).

Zwei sicherheitskritische Kontrollen werden hier bewiesen:

1. **Path-Traversal auf `entra_id`**: der Wert kommt aus dem URL-Pfad und wird zu einem
   Dateipfad. Vor JEDEM Dateisystemzugriff muss er gegen `^[A-Za-z0-9-]{1,64}$` validiert
   werden -- alles andere -> 404, kein Graph-Call, kein Verzeichnis angelegt.
2. **Tenant-Scoping des Caches**: derselbe `entra_id` unter zwei aktiven Mandanten landet in
   getrennten `{tenant_id}`-Verzeichnissen (kein Cross-Tenant-Serve).

Getestet wird -- wie in `test_switch_tenant.py`/`test_deps_guards.py` -- durch direkten
Aufruf der Service-/Guard-Funktionen mit gefälschtem `GraphClient` (monkeypatch), ohne
HTTP-Stack. Das `_avatars_root` des Service wird pro Test auf `tmp_path` umgebogen, damit
nie in das echte `data_dir` (`/data`) geschrieben wird.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.core.errors import AuthError, NotFoundError
from app.services import entra_avatar as avatar_service

_GUID = "11111111-2222-3333-4444-555555555555"

_SETTINGS = {
    "graph.tenant_id": "tid",
    "graph.client_id": "cid",
    "graph.client_secret": "secret",
    "graph.cloud": "global",
}


def _png_bytes() -> bytes:
    """Ein echtes, nicht-quadratisches PNG -- treibt auch den Crop/Resize-Pfad."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _install_fake_graph(
    monkeypatch: pytest.MonkeyPatch, *, photo: bytes | None = None, raises: bool = False
) -> dict[str, int]:
    """Ersetzt `GraphClient` im Service durch eine zählende Fake-Klasse und gibt den
    Aufrufzähler zurück (Beweis für Cache-Hits: EXACTLY-ONCE)."""
    calls = {"n": 0}

    class _FakeGraph:
        def __init__(self, config: object) -> None:
            self.config = config

        async def get_user_photo(self, user_id: str) -> bytes | None:
            calls["n"] += 1
            if raises:
                raise RuntimeError("graph down")
            return photo

    monkeypatch.setattr(avatar_service, "GraphClient", _FakeGraph)
    return calls


def _redirect_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "entra-avatars"
    monkeypatch.setattr(avatar_service, "_avatars_root", lambda: root)
    return root


# --------------------------------------------------------------------------- #
# Cache-Hit: zweiter Abruf ruft Graph NICHT erneut
# --------------------------------------------------------------------------- #
async def test_first_fetch_caches_png_second_is_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _redirect_cache(monkeypatch, tmp_path)
    calls = _install_fake_graph(monkeypatch, photo=_png_bytes())

    resp1 = await avatar_service.serve(_GUID, 7, _SETTINGS)
    assert resp1.media_type == "image/png"
    assert resp1.headers.get("cache-control") == "max-age=3600"

    cached = root / "7" / f"{_GUID}.png"
    assert cached.exists(), "PNG wurde nicht auf Platte gecacht"
    # Es ist ein echtes, verarbeitetes 256x256-PNG
    from PIL import Image

    assert Image.open(cached).size == (256, 256)

    resp2 = await avatar_service.serve(_GUID, 7, _SETTINGS)
    assert resp2.media_type == "image/png"
    assert calls["n"] == 1, f"Cache-Hit erwartet, Graph {calls['n']}x aufgerufen"


# --------------------------------------------------------------------------- #
# Negative-Cache: kein Foto -> .none-Sentinel, kein zweiter Graph-Call
# --------------------------------------------------------------------------- #
async def test_no_photo_writes_negative_sentinel_and_skips_second_graph_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _redirect_cache(monkeypatch, tmp_path)
    calls = _install_fake_graph(monkeypatch, photo=None)

    with pytest.raises(NotFoundError) as e1:
        await avatar_service.serve(_GUID, 7, _SETTINGS)
    assert e1.value.status_code == 404

    sentinel = root / "7" / f"{_GUID}.none"
    assert sentinel.exists(), ".none-Sentinel wurde nicht geschrieben"
    assert not (root / "7" / f"{_GUID}.png").exists()

    with pytest.raises(NotFoundError):
        await avatar_service.serve(_GUID, 7, _SETTINGS)
    assert calls["n"] == 1, "Negative-Cache-Hit sollte Graph NICHT erneut aufrufen"


# --------------------------------------------------------------------------- #
# Path-Traversal: bösartige entra_id -> 404, KEIN Graph-Call, KEINE Datei
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "a/b",
        "..",
        "foo.bar",
        "id;rm-rf",
        "..%2f..%2fetc",
        "with space",
        "a" * 65,  # zu lang
        "",  # leer
    ],
)
async def test_path_traversal_and_invalid_ids_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    root = _redirect_cache(monkeypatch, tmp_path)
    calls = _install_fake_graph(monkeypatch, photo=_png_bytes())

    with pytest.raises(NotFoundError):
        await avatar_service.serve(bad, 7, _SETTINGS)

    assert calls["n"] == 0, "Ungültige id darf Graph NIE erreichen"
    # Validierung schlägt VOR jedem Dateisystemzugriff zu: kein Cache-Verzeichnis angelegt.
    assert not root.exists(), "Bei ungültiger id darf nichts auf Platte angelegt werden"


def test_validator_regex_rejects_traversal_accepts_guid() -> None:
    assert avatar_service._ENTRA_ID_RE.fullmatch(_GUID) is not None
    for bad in ("../../etc/passwd", "a/b", "..", "a.b", "a b", "", "a" * 65):
        assert avatar_service._ENTRA_ID_RE.fullmatch(bad) is None, f"{bad!r} fälschlich akzeptiert"


# --------------------------------------------------------------------------- #
# Tenant-Scoping: gleicher entra_id, zwei Mandanten -> getrennte Verzeichnisse
# --------------------------------------------------------------------------- #
async def test_same_id_two_tenants_cache_under_separate_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _redirect_cache(monkeypatch, tmp_path)
    _install_fake_graph(monkeypatch, photo=_png_bytes())

    await avatar_service.serve(_GUID, 7, _SETTINGS)
    await avatar_service.serve(_GUID, 9, _SETTINGS)

    p7 = root / "7" / f"{_GUID}.png"
    p9 = root / "9" / f"{_GUID}.png"
    assert p7.exists() and p9.exists()
    assert p7 != p9, "Cache-Pfade zweier Mandanten dürfen nicht kollidieren"


async def test_none_active_tenant_is_404_without_graph_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_cache(monkeypatch, tmp_path)
    calls = _install_fake_graph(monkeypatch, photo=_png_bytes())

    with pytest.raises(NotFoundError):
        await avatar_service.serve(_GUID, None, _SETTINGS)
    assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# Graph-Transportfehler -> 404, NIE 500; Negative-Cache gesetzt
# --------------------------------------------------------------------------- #
async def test_graph_transport_error_is_404_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _redirect_cache(monkeypatch, tmp_path)
    _install_fake_graph(monkeypatch, raises=True)

    with pytest.raises(NotFoundError) as exc:
        await avatar_service.serve(_GUID, 7, _SETTINGS)
    assert exc.value.status_code == 404, "Graph-Fehler darf nie zu 500 werden"
    assert (root / "7" / f"{_GUID}.none").exists()


# --------------------------------------------------------------------------- #
# Unauthentifiziert -> 401 (der Guard, den die Route über CurrentUser erzwingt)
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookies = cookies


async def test_unauthenticated_request_is_rejected() -> None:
    from app.api.deps import get_current_user

    with pytest.raises(AuthError) as exc:
        await get_current_user(_FakeRequest({}), None)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
