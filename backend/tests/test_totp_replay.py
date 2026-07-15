"""Ein TOTP-Code darf nur einmal gelten.

Wegen ``valid_window=1`` ist ein Code rund 90 Sekunden gültig. Ohne Sperre liesse er sich
in dieser Zeit mehrfach einsetzen — wer ihn abfängt (Schulterblick, Mitschnitt, Malware),
käme damit ein zweites Mal hinein, obwohl der zweite Faktor genau das verhindern soll.
"""

from __future__ import annotations

import time

import pyotp
from app.core.twofa import matching_step, verify_totp

SECRET = pyotp.random_base32()


def test_current_code_yields_a_step() -> None:
    code = pyotp.TOTP(SECRET).now()
    assert matching_step(SECRET, code) is not None


def test_same_code_yields_the_same_step() -> None:
    """Kern des Replay-Schutzes: derselbe Code ergibt denselben Schritt und fliegt auf."""
    code = pyotp.TOTP(SECRET).now()
    erst = matching_step(SECRET, code)
    zweit = matching_step(SECRET, code)
    assert erst == zweit is not None


def test_wrong_code_has_no_step() -> None:
    assert matching_step(SECRET, "000000") is None
    assert matching_step(SECRET, "") is None
    assert matching_step(SECRET, "abcdef") is None


def test_previous_window_is_still_accepted() -> None:
    """Uhr-Drift-Toleranz muss erhalten bleiben — sonst scheitern legitime Anmeldungen."""
    vorher = pyotp.TOTP(SECRET).at(int(time.time()) - 30)
    schritt = matching_step(SECRET, vorher)
    assert schritt is not None
    assert schritt == matching_step(SECRET, pyotp.TOTP(SECRET).now()) - 1


def test_consistent_with_verify_totp() -> None:
    """Beide Prüfungen müssen dasselbe Fenster akzeptieren, sonst gibt es Fehlalarme."""
    for code in (pyotp.TOTP(SECRET).now(), "000000", "xyz"):
        assert verify_totp(SECRET, code) == (matching_step(SECRET, code) is not None)


def test_steps_differ_between_windows() -> None:
    t = pyotp.TOTP(SECRET)
    jetzt = int(time.time())
    a = matching_step(SECRET, t.at(jetzt))
    b = matching_step(SECRET, t.at(jetzt - 30))
    assert a is not None and b is not None and a != b
