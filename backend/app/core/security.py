"""Passwort-Hashing (Argon2id) und JWT (Access/Refresh mit Rotation).

Der JWT-Signierschlüssel wird aus dem Fernet-Master-Key abgeleitet (HMAC-SHA256),
sodass kein zusätzliches Secret verwaltet werden muss.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import get_settings
from .crypto import resolve_secret_key

_ph = PasswordHasher()  # Argon2id mit sicheren Defaults
_ALG = "HS256"


# --------------------------------------------------------------------------- #
# Passwörter
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError, InvalidHashError:
        return False


_POLICY_UPPER = re.compile(r"[A-Z]")
_POLICY_LOWER = re.compile(r"[a-z]")
_POLICY_DIGIT = re.compile(r"[0-9]")
_POLICY_SPECIAL = re.compile(r"[^A-Za-z0-9]")


def password_meets_policy(password: str) -> bool:
    """Serverseitige Passwort-Policy für Einladung/Reset (Task 5, §7b/§7c) -- die einzige
    Autorität. Die Frontend-Checkliste (`frontend/src/lib/password.ts`) spiegelt exakt
    dieselben Regeln, ist aber NUR UX-Vorschau; ein Client, der sie umgeht, scheitert
    trotzdem hier: mind. 10 Zeichen, je ein Gross-/Kleinbuchstabe, eine Ziffer, ein
    Sonderzeichen (alles ausser [A-Za-z0-9])."""
    return (
        len(password) >= 10
        and bool(_POLICY_UPPER.search(password))
        and bool(_POLICY_LOWER.search(password))
        and bool(_POLICY_DIGIT.search(password))
        and bool(_POLICY_SPECIAL.search(password))
    )


def needs_rehash(hashed: str) -> bool:
    try:
        return _ph.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- #
# Kontosperre nach Fehlversuchen
#
# Gilt bewusst für BEIDE Faktoren. Zählt nur das Passwort, bleibt der zweite Faktor
# unbegrenzt ratbar — wer das Passwort schon hat, bräuchte dann nur Zeit.
# --------------------------------------------------------------------------- #
class _Lockable(Protocol):
    """Was zum Sperren nötig ist — hält die Funktionen von AppUser entkoppelt."""

    failed_login_count: int
    locked_until: dt.datetime | None


def register_failed_attempt(
    user: _Lockable, *, now: dt.datetime, max_failures: int, lockout_min: int
) -> bool:
    """Zählt einen Fehlversuch und sperrt bei Bedarf. Gibt zurück, ob jetzt gesperrt ist."""
    user.failed_login_count += 1
    if user.failed_login_count >= max_failures:
        user.locked_until = now + dt.timedelta(minutes=lockout_min)
        user.failed_login_count = 0
        return True
    return False


def reset_failed_attempts(user: _Lockable) -> None:
    """Nach erfolgreicher Anmeldung: Zähler und Sperre löschen."""
    user.failed_login_count = 0
    user.locked_until = None


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def _signing_key() -> bytes:
    # Aus dem Master-Key abgeleiteter, stabiler HMAC-Schlüssel für JWT.
    return hmac.new(resolve_secret_key(), b"pwnotify-jwt-v1", hashlib.sha256).digest()


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    refresh_jti: str
    access_expires: dt.datetime
    refresh_expires: dt.datetime


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def create_access_token(
    subject: str, *, extra: dict[str, Any] | None = None
) -> tuple[str, dt.datetime]:
    settings = get_settings()
    exp = _now() + dt.timedelta(minutes=settings.access_token_ttl_min)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
        **(extra or {}),
    }
    return jwt.encode(payload, _signing_key(), algorithm=_ALG), exp


def create_refresh_token(subject: str, *, jti: str | None = None) -> tuple[str, str, dt.datetime]:
    settings = get_settings()
    token_jti = jti or uuid.uuid4().hex
    exp = _now() + dt.timedelta(days=settings.refresh_token_ttl_days)
    payload = {
        "sub": subject,
        "type": "refresh",
        "jti": token_jti,
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _signing_key(), algorithm=_ALG), token_jti, exp


def create_2fa_token(subject: str) -> str:
    """Kurzlebiger Zwischen-Token nach Passwort-OK, vor 2FA-Code (Typ '2fa', 5 min)."""
    exp = _now() + dt.timedelta(minutes=5)
    payload = {
        "sub": subject,
        "type": "2fa",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _signing_key(), algorithm=_ALG)


def issue_token_pair(subject: str, *, active_tenant: int | None = None) -> TokenPair:
    extra = {"active_tenant": active_tenant} if active_tenant is not None else None
    access, a_exp = create_access_token(subject, extra=extra)
    refresh, jti, r_exp = create_refresh_token(subject)
    return TokenPair(access, refresh, jti, a_exp, r_exp)


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(token, _signing_key(), algorithms=[_ALG])
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"erwarteter Token-Typ {expected_type}")
    return payload


def hash_token(token: str) -> str:
    """Refresh-Tokens werden nur als Hash in der DB abgelegt (kein Klartext)."""
    return hashlib.sha256(token.encode()).hexdigest()
