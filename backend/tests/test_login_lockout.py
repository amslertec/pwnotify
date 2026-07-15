"""Kontosperre nach zu vielen Fehlversuchen — für Passwort UND 2FA-Code.

Der zweite Faktor war ungeschützt: Ein falscher TOTP-Code erhöhte den Fehlerzähler nicht
und sperrte nie. Wer das Passwort bereits hat (Phishing, Leak), konnte den sechsstelligen
Code also beliebig oft raten; das IP-Rate-Limit allein hält einen Angreifer mit mehreren
Adressen nicht auf. Damit wäre der zweite Faktor faktisch wirkungslos gewesen.
"""

from __future__ import annotations

import datetime as dt

from app.core.security import register_failed_attempt, reset_failed_attempts

NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC)


class FakeUser:
    def __init__(self) -> None:
        self.failed_login_count = 0
        self.locked_until: dt.datetime | None = None


def test_single_failure_does_not_lock() -> None:
    u = FakeUser()
    assert register_failed_attempt(u, now=NOW, max_failures=5, lockout_min=15) is False
    assert u.failed_login_count == 1
    assert u.locked_until is None


def test_lock_after_max_failures() -> None:
    u = FakeUser()
    for _ in range(4):
        assert register_failed_attempt(u, now=NOW, max_failures=5, lockout_min=15) is False
    assert register_failed_attempt(u, now=NOW, max_failures=5, lockout_min=15) is True
    assert u.locked_until == NOW + dt.timedelta(minutes=15)
    # Zähler zurücksetzen, damit nach Ablauf der Sperre wieder voll gezählt wird.
    assert u.failed_login_count == 0


def test_reset_clears_counter_and_lock() -> None:
    u = FakeUser()
    register_failed_attempt(u, now=NOW, max_failures=5, lockout_min=15)
    u.locked_until = NOW
    reset_failed_attempts(u)
    assert u.failed_login_count == 0
    assert u.locked_until is None


def test_totp_guessing_gets_locked_out() -> None:
    """Der eigentliche Angriff: Passwort bekannt, Code wird geraten."""
    u = FakeUser()
    versuche = 0
    locked = False
    while not locked and versuche < 20:
        versuche += 1
        locked = register_failed_attempt(u, now=NOW, max_failures=5, lockout_min=15)
    assert locked, "TOTP-Raten muss zur Sperre führen"
    assert versuche == 5
