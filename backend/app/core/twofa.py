"""TOTP-2FA-Helfer: Secret/QR erzeugen, Codes prüfen, Recovery-Codes."""

from __future__ import annotations

import base64
import hashlib
import io
import secrets

import pyotp
import qrcode

_ISSUER = "PwNotify"


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=_ISSUER)


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    # valid_window=1 -> ±30s Toleranz gegen Uhr-Drift.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def qr_png_data_uri(uri: str) -> str:
    """otpauth-URI als QR-PNG (Data-URI) rendern (nutzt Pillow)."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def generate_recovery_codes(n: int = 10) -> tuple[list[str], list[str]]:
    """Gibt (Klartext-Codes zum einmaligen Anzeigen, SHA-256-Hashes zum Speichern) zurück."""
    codes = [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(n)
    ]
    return codes, [_hash_code(c) for c in codes]


def match_recovery_code(code: str, hashes: list[str]) -> str | None:
    """Prüft einen eingegebenen Recovery-Code gegen die Hash-Liste; gibt den Treffer-Hash zurück."""
    h = _hash_code((code or "").strip().lower())
    return h if h in hashes else None
