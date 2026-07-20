"""Aufbewahrungsfristen für personenbezogene Daten.

PwNotify spiegelt Entra-Konten (Name, UPN, Mailadressen) und protokolliert jeden Versand.
Ohne Fristen wächst beides unbegrenzt: Wer den Tenant längst verlassen hat, bleibt mit
allen Daten gespeichert, und die Versandhistorie hält Mailadressen für immer fest. Bei
1000+ Konten ist das ein Datenschutzthema, kein Schönheitsfehler.

Alle Fristen sind standardmässig **aus** (0). Wer sie einschaltet, entscheidet bewusst —
Löschen ist nicht rückgängig zu machen.
"""

from __future__ import annotations

# Unterhalb dieser Menge wird nie blockiert: bei wenigen Konten ist ein hoher Anteil
# normal und harmlos.
_MIN_COUNT = 20
# Mehr als die Hälfte auf einmal zu löschen deutet auf einen Fehler hin, nicht auf Abgänge.
_MAX_RATIO = 0.5

# Non-erasable floor for the audit-log retention window (M3). The audit trail must always
# keep at least this many days of recent history; 0 (keep forever) stays allowed, but any
# positive window is treated as at least this many days.
#
# Why a hard floor instead of a stateful cumulative-window brake: an admin covering their
# tracks lowers ``audit.retention_days`` in small steps and triggers a purge each time. Every
# step stays under the >50% brake -- and slices below ``_MIN_COUNT`` bypass it entirely -- so
# the trail (including the SETTINGS_CHANGED entries that document the shrinking) drains after a
# few iterations. A tracked "max deleted per rolling time window" brake would stop that but
# needs persistent purge state across runs. The floor reaches the same goal directly and
# statelessly: the most recent FLOOR days can never be purged, so the tamper evidence always
# survives. A stateful window brake was deliberately NOT built (YAGNI).
AUDIT_RETENTION_FLOOR_DAYS = 30


def purge_blocked_reason(*, to_delete: int, total: int) -> str | None:
    """Prüft, ob eine geplante Löschung plausibel ist. Grund, wenn nicht — sonst ``None``.

    Der entscheidende Fehlerfall: Schlägt der Graph-Sync über Tage fehl, altert
    ``last_synced_at`` bei **allen** Konten gleichzeitig. Eine naive Frist würde dann den
    kompletten Bestand löschen — verursacht durch eine Störung, nicht durch Abgänge.
    Im Zweifel wird nichts gelöscht: ein Datensatz zu viel ist reparabel, tausend
    gelöschte nicht.
    """
    if to_delete <= 0 or total <= 0:
        return None
    if to_delete < _MIN_COUNT:
        return None
    if to_delete > total * _MAX_RATIO:
        return (
            f"Die Aufbewahrungsfrist würde {to_delete} von {total} Datensätzen entfernen "
            f"({to_delete / total:.0%}). Das deutet auf eine Störung hin — etwa einen "
            "fehlgeschlagenen Sync, nach dem alle Einträge gleich alt wirken. Es wurde "
            "nichts gelöscht. Bitte Frist und letzten Lauf prüfen."
        )
    return None
