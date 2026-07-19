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

    # Auto-generation on the very first start. Create the directory and file
    # restrictively so there is no window in which the key would be readable by
    # other local processes/users (previously: write_bytes() with the default
    # umask, chmod only afterwards -- a brief 0644 window).
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # os.open() sets the mode right at creation time (minus umask), and
        # O_EXCL prevents overwriting a key created concurrently by another process.
        fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    except FileExistsError:
        # A concurrent first start won the race -- adopt its key.
        return [key_path.read_bytes().strip()]
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    # os.open()'s mode gets masked by the process umask (mode & ~umask); a
    # permissive umask (e.g. 0022) would otherwise yield 0644 instead of 0600.
    # The explicit chmod only ever narrows permissions, it never opens a window.
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
