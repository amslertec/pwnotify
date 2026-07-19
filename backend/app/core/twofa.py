"""TOTP-2FA-Helfer: Secret/QR erzeugen, Codes prüfen, Recovery-Codes."""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import time

import pyotp
import qrcode

from .security import hash_password, verify_password

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


def matching_step(secret: str, code: str, *, now: float | None = None) -> int | None:
    """Zu welchem Zeitschritt gehört der Code? ``None``, wenn er nicht passt.

    Grundlage des Replay-Schutzes: Ein TOTP-Code ist wegen ``valid_window=1`` rund 90 s
    lang gültig und liesse sich in der Zeit mehrfach verwenden. Wer ihn abfängt
    (Schulterblick, Mitschnitt), käme damit ein zweites Mal hinein. Der Aufrufer merkt
    sich den zurückgegebenen Schritt und lehnt ihn beim nächsten Mal ab.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    jetzt = now if now is not None else time.time()
    # Gleiches Fenster wie verify_totp: aktueller Schritt ± 1.
    aktuell = int(jetzt // totp.interval)
    for schritt in (aktuell, aktuell - 1, aktuell + 1):
        if secrets.compare_digest(totp.at(schritt * totp.interval), code):
            return schritt
    return None


def qr_png_data_uri(uri: str) -> str:
    """otpauth-URI als QR-PNG (Data-URI) rendern (nutzt Pillow)."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _hash_code(code: str) -> str:
    """Legacy hash (unsalted SHA-256) — kept only to verify old codes, no longer used
    to generate new ones (see `generate_recovery_codes`)."""
    return hashlib.sha256(code.encode()).hexdigest()


def generate_recovery_codes(n: int = 10) -> tuple[list[str], list[str]]:
    """Returns (plaintext codes for one-time display, Argon2id hashes for storage).

    5 groups of `token_hex(2)` = 80 bits of entropy (previously 3 groups = 48 bits).
    Stored as an Argon2id hash (`hash_password`), consistent with password hashing
    elsewhere in the app.
    """
    codes = ["-".join(secrets.token_hex(2) for _ in range(5)) for _ in range(n)]
    return codes, [hash_password(c) for c in codes]


def match_recovery_code(code: str, hashes: list[str]) -> str | None:
    """Checks an entered recovery code against the hash list; returns the matching hash.

    Format-coexistent: recognizes both new Argon2id hashes (self-describing via the
    `$argon2` prefix) and existing legacy SHA-256 hashes (64 hex chars). Necessary
    because recovery codes can't be re-derived — existing SHA-256 codes must stay
    verifiable until the user re-enrolls in 2FA.
    """
    normalized = (code or "").strip().lower()
    legacy_hash = _hash_code(normalized)
    for h in hashes:
        if h.startswith("$argon2"):
            if verify_password(normalized, h):
                return h
        elif secrets.compare_digest(legacy_hash, h):
            return h
    return None
