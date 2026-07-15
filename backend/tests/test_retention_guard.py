"""Schutz der Aufbewahrungsfristen gegen Massenlöschung.

Der gefährliche Fall: Schlägt der Graph-Sync über Tage fehl, altert `last_synced_at` bei
allen Konten gleichzeitig. Eine naive Frist würde dann den kompletten Bestand löschen —
ausgelöst durch eine Störung, nicht durch echte Abgänge. Gelöschte Konten sind nicht
zurückholbar.
"""

from __future__ import annotations

from app.services.retention import purge_blocked_reason


def test_normal_departures_are_allowed() -> None:
    """Der Alltag: ein paar Leute verlassen die Firma."""
    assert purge_blocked_reason(to_delete=30, total=1000) is None


def test_mass_deletion_is_blocked() -> None:
    """Sync kaputt -> alle wirken veraltet -> muss blockiert werden."""
    grund = purge_blocked_reason(to_delete=1000, total=1000)
    assert grund is not None
    assert "1000" in grund


def test_small_tenants_are_never_blocked() -> None:
    """3 von 5 Konten weg ist bei kleinen Beständen plausibel."""
    assert purge_blocked_reason(to_delete=3, total=5) is None


def test_nothing_to_delete() -> None:
    assert purge_blocked_reason(to_delete=0, total=1000) is None


def test_empty_database() -> None:
    assert purge_blocked_reason(to_delete=0, total=0) is None


def test_boundary_is_allowed() -> None:
    """Genau die Hälfte darf noch, erst darüber wird abgebrochen."""
    assert purge_blocked_reason(to_delete=500, total=1000) is None
    assert purge_blocked_reason(to_delete=501, total=1000) is not None
