"""Warnung vor Ablauf des Graph-Client-Secrets.

Läuft das Secret ab, steht das Tool still: Der Sync scheitert und es gehen keine
Erinnerungen mehr raus — ein Ausfall, der niemandem auffällt, weil ausbleibende Mails
nicht auffallen. Deshalb muss vorher gewarnt werden.
"""

from __future__ import annotations

import datetime as dt

from app.services.secret_expiry import ALERT_DAYS, WARN_DAYS, check

HEUTE = dt.date(2026, 7, 15)


def test_no_date_configured() -> None:
    """Das Feld ist optional — ohne Datum gibt es einfach keine Warnung."""
    assert check(None, today=HEUTE) is None
    assert check("", today=HEUTE) is None


def test_broken_date_is_ignored() -> None:
    """Eine kaputte Eingabe darf weder Sync noch Dashboard kippen."""
    assert check("übermorgen", today=HEUTE) is None
    assert check("31.01.2027", today=HEUTE) is None  # kein ISO-Format


def test_far_future_does_not_warn() -> None:
    r = check("2027-01-31", today=HEUTE)
    assert r is not None
    assert r.days_left == 200
    assert not r.should_warn and not r.should_alert and not r.expired


def test_warns_within_30_days() -> None:
    r = check(str(HEUTE + dt.timedelta(days=WARN_DAYS)), today=HEUTE)
    assert r is not None and r.should_warn and not r.should_alert


def test_alerts_within_14_days() -> None:
    r = check(str(HEUTE + dt.timedelta(days=ALERT_DAYS)), today=HEUTE)
    assert r is not None and r.should_warn and r.should_alert


def test_expired_is_detected() -> None:
    r = check("2026-07-01", today=HEUTE)
    assert r is not None
    assert r.expired and r.days_left == -14
    assert r.should_warn and r.should_alert


def test_accepts_full_timestamp() -> None:
    """Aus Entra kopiert man gern den vollen Zeitstempel — der muss auch gehen."""
    r = check("2026-08-01T23:59:59Z", today=HEUTE)
    assert r is not None and r.expires_at == dt.date(2026, 8, 1)
