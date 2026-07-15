"""Warnung vor dem Ablauf des Graph-Client-Secrets.

Entra-Client-Secrets laufen nach 6 bis 24 Monaten ab. Passiert das unbemerkt, steht das Tool
still: Der Sync scheitert, es gehen keine Erinnerungen mehr raus — und niemand merkt es,
weil ein Ausbleiben von Mails nicht auffällt. Das Datum wird manuell gepflegt (siehe
``settings_schema``), hier wird daraus eine rechtzeitige Warnung.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Ab hier im Dashboard sichtbar warnen …
WARN_DAYS = 30
# … und ab hier zusätzlich den Admin-Alert auslösen (Zeit genug, ein neues Secret zu
# erzeugen und einzutragen, ohne Betriebsunterbrechung).
ALERT_DAYS = 14


@dataclass(frozen=True)
class SecretExpiry:
    """Ergebnis der Prüfung. ``days_left`` ist negativ, wenn bereits abgelaufen."""

    expires_at: dt.date
    days_left: int

    @property
    def expired(self) -> bool:
        return self.days_left < 0

    @property
    def should_warn(self) -> bool:
        return self.days_left <= WARN_DAYS

    @property
    def should_alert(self) -> bool:
        return self.days_left <= ALERT_DAYS


def check(raw: str | None, *, today: dt.date | None = None) -> SecretExpiry | None:
    """Wertet das eingetragene Ablaufdatum aus. ``None``, wenn keins oder unlesbar.

    Ein unlesbares Datum wird bewusst ignoriert statt zu einem Fehler zu führen: Das Feld
    ist optional, und eine kaputte Eingabe darf weder den Sync noch das Dashboard kippen.
    """
    if not raw:
        return None
    try:
        expires = dt.date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None
    heute = today or dt.datetime.now(dt.UTC).date()
    return SecretExpiry(expires_at=expires, days_left=(expires - heute).days)
