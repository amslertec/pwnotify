"""Schutz davor, dass ein SSO-Sync die eigenen Administratoren aussperrt.

Hintergrund: `sync_sso_users` löscht jeden SSO-Benutzer, der nicht mehr in der Admin-
oder Auditor-Gruppe ist. Liefert Graph für eine *existierende, aber leere* Gruppe eine
leere Liste (leergeräumte Gruppe, falsche Group-ID), ist die Soll-Menge leer — und ohne
Schutz werden sämtliche SSO-Admins entfernt. Wer nur per SSO anmeldet, kommt dann nicht
mehr in die eigene Anwendung.
"""

from __future__ import annotations

from app.services.oidc import removal_blocked_reason


def test_empty_desired_set_blocks_removal() -> None:
    """Der Kernfall: leere Gruppe darf niemals alle SSO-Benutzer löschen."""
    reason = removal_blocked_reason(desired_count=0, existing_count=3, removal_count=3)
    assert reason is not None
    assert "leer" in reason.lower()


def test_mass_removal_is_blocked() -> None:
    """Ein Sync, der die Mehrheit entfernen will, ist verdächtig — nicht ausführen."""
    assert removal_blocked_reason(desired_count=1, existing_count=10, removal_count=9) is not None


def test_normal_removal_is_allowed() -> None:
    """Einzelne Abgänge sind der Normalfall und müssen durchgehen."""
    assert removal_blocked_reason(desired_count=9, existing_count=10, removal_count=1) is None


def test_nothing_to_remove_is_allowed() -> None:
    assert removal_blocked_reason(desired_count=5, existing_count=5, removal_count=0) is None


def test_first_run_without_existing_users_is_allowed() -> None:
    """Erstlauf: noch keine SSO-Benutzer vorhanden — nichts zu schützen."""
    assert removal_blocked_reason(desired_count=0, existing_count=0, removal_count=0) is None


def test_half_removal_is_still_allowed() -> None:
    """Genau die Hälfte ist die Grenze und darf noch laufen."""
    assert removal_blocked_reason(desired_count=5, existing_count=10, removal_count=5) is None
