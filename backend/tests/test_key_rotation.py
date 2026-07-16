"""Schlüsselwechsel ohne Neueingabe der Secrets.

Bisher gab es genau einen Fernet-Schlüssel: Ein kompromittierter oder abzulösender
Schlüssel liess sich nur ersetzen, indem man jedes Secret (Graph, SMTP, TOTP) neu
eintippt — und bis dahin sahen alle Werte aus wie "nicht konfiguriert".

Mehrere Schlüssel werden kommagetrennt übergeben. Verschlüsselt wird mit dem ersten,
entschlüsselt mit dem ersten passenden.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core import crypto
from cryptography.fernet import Fernet

ALT = Fernet.generate_key().decode()
NEU = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    """Die Fernet-Instanz ist gecacht — sonst wirkt ein Schlüsselwechsel im Test nicht."""
    crypto._fernet.cache_clear()
    crypto.get_settings.cache_clear()
    yield
    crypto._fernet.cache_clear()
    crypto.get_settings.cache_clear()


def _keys(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PWNOTIFY_SECRET_KEY", value)
    crypto.get_settings.cache_clear()
    crypto._fernet.cache_clear()


def test_single_key_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, ALT)
    assert crypto.decrypt(crypto.encrypt("geheim")) == "geheim"


def test_old_value_stays_readable_after_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Kern: Mit dem alten Schlüssel Geschriebenes bleibt nach dem Wechsel lesbar."""
    _keys(monkeypatch, ALT)
    alt_verschluesselt = crypto.encrypt("graph-secret")

    _keys(monkeypatch, f"{NEU},{ALT}")  # neuer Schlüssel voran, alter bleibt
    assert crypto.decrypt(alt_verschluesselt) == "graph-secret"


def test_new_writes_use_the_first_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neues wird mit dem neuen Schlüssel geschrieben — sonst käme man nie vom alten los."""
    _keys(monkeypatch, f"{NEU},{ALT}")
    frisch = crypto.encrypt("neuer-wert")

    _keys(monkeypatch, NEU)  # alter Schlüssel entfernt
    assert crypto.decrypt(frisch) == "neuer-wert"


def test_removing_the_old_key_locks_out_old_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ehrlichkeit über die Konsequenz: Zu früh entfernt = alte Werte sind weg."""
    _keys(monkeypatch, ALT)
    alt_verschluesselt = crypto.encrypt("wert")

    _keys(monkeypatch, NEU)
    with pytest.raises(ValueError):
        crypto.decrypt(alt_verschluesselt)


def test_signing_key_is_the_active_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der JWT-Signierschlüssel folgt dem ersten — sonst flögen bei jedem Wechsel
    alle Sitzungen raus."""
    _keys(monkeypatch, f"{NEU},{ALT}")
    assert crypto.resolve_secret_key() == NEU.encode()
    assert crypto.resolve_secret_keys() == [NEU.encode(), ALT.encode()]


def test_whitespace_between_keys_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, f" {NEU} , {ALT} ")
    assert crypto.resolve_secret_keys() == [NEU.encode(), ALT.encode()]
