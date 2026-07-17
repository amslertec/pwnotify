"""Einmal-Tokens für Konto-Aktionen ohne aktive Session (Einladung, Passwort-Reset)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String
from sqlmodel import Field, SQLModel

from ._base import utcnow


class UserToken(SQLModel, table=True):
    """Generalisiertes Einmal-Token: bedient sowohl Einladung ('invite') als auch
    Passwort-Reset ('reset') -- beide brauchen dieselbe Form (Hash + Ablauf + Single-Use),
    nur `purpose` und die vom Service gesetzte `expires_at`-Spanne unterscheiden sich
    (Einladung ~7 Tage, Reset ~1 Stunde; das setzt der Service, Task 5, nicht diese Tabelle).

    Bewusst KEINE eigene `email`-Spalte: die Zieladresse liegt bereits auf
    `app_user.email` (beim eingeladenen Konto von Anfang an gesetzt, beim Reset die
    bestehende Adresse des Kontos) -- eine Quelle der Wahrheit statt zwei, die
    auseinanderlaufen könnten.

    Nur `token_hash` wird gespeichert (sha256 hex), nie der Klartext -- exakt wie bei
    `UserSession.token_hash`. `consumed_at IS NULL` heisst "noch gültig/einlösbar";
    ein gesetzter Wert macht das Token endgültig unbrauchbar (Single-Use-Flag).
    """

    __tablename__ = "user_token"

    id: int | None = Field(default=None, primary_key=True)
    app_user_id: int = Field(foreign_key="app_user.id", index=True, nullable=False)
    purpose: str = Field(sa_column=Column(String(16), nullable=False))
    token_hash: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    expires_at: dt.datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    consumed_at: dt.datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_by: int = Field(foreign_key="app_user.id", nullable=False)
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
