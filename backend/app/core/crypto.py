"""At-rest-Verschlüsselung geheimer Settings (Fernet) + Master-Key-Auflösung.

Master-Key-Quelle (in dieser Reihenfolge):
1. ENV ``PWNOTIFY_SECRET_KEY`` (empfohlen für Multi-Node / externes Secret-Mgmt)
2. Datei ``{data_dir}/secret.key`` (wird beim ersten Start mit 0600 erzeugt)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def resolve_secret_key() -> bytes:
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key.encode()

    key_path = Path(settings.data_dir) / "secret.key"
    if key_path.exists():
        return key_path.read_bytes().strip()

    # Auto-Generierung beim allerersten Start
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return key


@lru_cache
def _fernet() -> Fernet:
    return Fernet(resolve_secret_key())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensiv
        raise ValueError("Secret konnte nicht entschlüsselt werden (falscher Key?)") from exc
