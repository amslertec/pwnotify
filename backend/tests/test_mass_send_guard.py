"""Sicherung gegen Massen-Fehlversand.

Die Versandschleife kannte kein Limit: Eine falsche Gültigkeitsdauer lässt schlagartig
alle Benutzer als „fällig“ erscheinen — bei einem Kunden mit 1000+ Konten gehen dann 1000
Mails raus, bevor es jemand merkt. Das ist nicht rückholbar.
"""

from __future__ import annotations

from app.services.runner import mass_send_blocked_reason


def test_normal_run_is_not_blocked() -> None:
    """Der Alltag: ein paar Stichtage von vielen Benutzern."""
    assert mass_send_blocked_reason(due=55, checked=1000, max_ratio=0.5) is None


def test_misconfiguration_blocks_the_run() -> None:
    """Falsche Gültigkeitsdauer -> alle fällig -> abbrechen."""
    reason = mass_send_blocked_reason(due=1000, checked=1000, max_ratio=0.5)
    assert reason is not None
    assert "1000" in reason


def test_small_tenants_are_never_blocked() -> None:
    """5 Benutzer, 3 fällig = 60 % — echt, aber harmlos. Nicht blockieren."""
    assert mass_send_blocked_reason(due=3, checked=5, max_ratio=0.5) is None


def test_guard_can_be_disabled() -> None:
    assert mass_send_blocked_reason(due=1000, checked=1000, max_ratio=0.0) is None


def test_nothing_due_is_not_blocked() -> None:
    assert mass_send_blocked_reason(due=0, checked=1000, max_ratio=0.5) is None


def test_boundary_is_allowed() -> None:
    """Genau auf der Schwelle darf laufen — erst darüber wird abgebrochen."""
    assert mass_send_blocked_reason(due=500, checked=1000, max_ratio=0.5) is None
    assert mass_send_blocked_reason(due=501, checked=1000, max_ratio=0.5) is not None


def test_no_users_checked_is_safe() -> None:
    assert mass_send_blocked_reason(due=0, checked=0, max_ratio=0.5) is None
