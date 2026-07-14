"""Reine Passwort-Ablauf-Berechnung (keine I/O -> unit-testbar).

``daysLeft`` wird als Kalendertage-Differenz berechnet (passend zum täglichen
Scheduler). Negative Werte = bereits abgelaufen. ``never_expires`` deckt die
Entra-Policy ``DisablePasswordExpiration`` sowie fehlende/0 Gültigkeitsdauer ab.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

DISABLE_EXPIRATION_POLICY = "DisablePasswordExpiration"


@dataclass(frozen=True)
class ExpiryResult:
    never_expires: bool
    expiry_date: dt.datetime | None
    days_left: int | None

    @property
    def cycle(self) -> str | None:
        """Identifiziert den Ablaufzyklus (für Dedup). Ändert sich bei Passwortwechsel."""
        return self.expiry_date.date().isoformat() if self.expiry_date else None


def compute_expiry(
    *,
    last_password_change: dt.datetime | None,
    validity_days: int | None,
    password_policies: str | None,
    now: dt.datetime | None = None,
) -> ExpiryResult:
    now = now or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)

    policies = password_policies or ""
    never = DISABLE_EXPIRATION_POLICY.lower() in policies.lower()
    if never or not validity_days or validity_days <= 0:
        return ExpiryResult(never_expires=True, expiry_date=None, days_left=None)

    if last_password_change is None:
        # Ohne Basisdatum kein Ablauf berechenbar -> unbekannt (nicht "läuft nie ab").
        return ExpiryResult(never_expires=False, expiry_date=None, days_left=None)

    if last_password_change.tzinfo is None:
        last_password_change = last_password_change.replace(tzinfo=dt.UTC)

    expiry = last_password_change + dt.timedelta(days=validity_days)
    days_left = (expiry.date() - now.date()).days
    return ExpiryResult(never_expires=False, expiry_date=expiry, days_left=days_left)


def due_reminder_stage(
    *, days_left: int | None, reminder_days: list[int], already_sent: set[int]
) -> int | None:
    """Bestimmt die fällige Reminder-Stufe (max. eine pro Lauf).

    Die aktuell passende Stufe ist die **kleinste** konfigurierte Schwelle ``d``
    mit ``days_left <= d`` (positionsbasiert): bei 10 Resttagen ist das die
    14-Tage-Stufe, ab <=7 die 7-Tage-Stufe usw. Sie wird nur gesendet, wenn sie
    für diesen Zyklus noch nicht gesendet wurde. Dadurch werden ausgefallene
    Läufe nachgeholt (die Stufe richtet sich nach der aktuellen Restzeit), ohne
    weniger dringende Stufen nachträglich erneut zu verschicken.
    """
    if days_left is None:
        return None
    eligible = [d for d in reminder_days if days_left <= d]
    if not eligible:
        return None
    target = min(eligible)
    return target if target not in already_sent else None
