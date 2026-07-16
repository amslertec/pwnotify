"""At-rest-Verschlüsselung geheimer Settings (Fernet) + Master-Key-Auflösung.

Master-Key-Quelle (in dieser Reihenfolge):
1. ENV ``PWNOTIFY_SECRET_KEY`` (empfohlen für Multi-Node / externes Secret-Mgmt)
2. Datei ``{data_dir}/secret.key`` (wird beim ersten Start mit 0600 erzeugt)

Schlüsselwechsel: ``PWNOTIFY_SECRET_KEY`` nimmt mehrere Schlüssel kommagetrennt entgegen.
Verschlüsselt wird immer mit dem **ersten**, entschlüsselt mit dem ersten passenden. Ein
Wechsel läuft damit ohne Ausfall und ohne Neueingabe der Secrets:

1. neuen Schlüssel voranstellen:  ``PWNOTIFY_SECRET_KEY=<neu>,<alt>``  -> neu starten
   (alles bleibt lesbar, neu Geschriebenes nutzt bereits den neuen Schlüssel)
2. bestehende Werte umschlüsseln: in den Einstellungen einmal speichern, oder warten,
   bis sie ohnehin geändert werden
3. alten Schlüssel entfernen:     ``PWNOTIFY_SECRET_KEY=<neu>``

Ohne diese Möglichkeit wäre ein kompromittierter Schlüssel nur durch Neueingabe aller
Secrets zu ersetzen.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import get_settings


def resolve_secret_keys() -> list[bytes]:
    """Alle konfigurierten Schlüssel. Der erste ist der aktive (zum Verschlüsseln)."""
    settings = get_settings()
    if settings.secret_key:
        keys = [k.strip().encode() for k in settings.secret_key.split(",") if k.strip()]
        if keys:
            return keys

    key_path = Path(settings.data_dir) / "secret.key"
    if key_path.exists():
        return [key_path.read_bytes().strip()]

    # Auto-Generierung beim allerersten Start
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return [key]


def resolve_secret_key() -> bytes:
    """Der aktive Schlüssel. Auch Basis für die JWT-Signatur (HMAC-abgeleitet).

    Bewusst nur der erste: Beim Schlüsselwechsel würden sonst alle Sitzungen ungültig,
    weil sich der abgeleitete Signierschlüssel mitänderte.
    """
    return resolve_secret_keys()[0]


@lru_cache
def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(k) for k in resolve_secret_keys()])


def encrypt(value: str) -> str:
    """Verschlüsselt mit dem ersten (aktiven) Schlüssel."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """Entschlüsselt mit dem ersten passenden Schlüssel — so bleiben alte Werte lesbar."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Secret konnte nicht entschlüsselt werden (falscher Key?)") from exc
