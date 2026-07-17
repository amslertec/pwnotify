"""Revisionssicheres Protokoll sicherheitsrelevanter Aktionen.

Ohne dieses Protokoll lässt sich nicht beantworten, wer wann Administratorrechte vergeben,
2FA abgeschaltet oder das Graph-Secret ausgetauscht hat — die erste Frage in jedem
Compliance-Gespräch und die Grundlage jeder Nachbearbeitung eines Vorfalls.

Einträge werden nur geschrieben, nie geändert oder gelöscht. Der Name des Handelnden wird
mitkopiert (statt nur der Fremdschlüssel), damit die Spur lesbar bleibt, wenn das Konto
später entfernt wird.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..db.tenant_context import current_tenant_or_none
from ._base import utcnow


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int | None = Field(
        default_factory=current_tenant_or_none, foreign_key="tenant.id", index=True
    )
    at: dt.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )

    # Wer? Bewusst ohne Fremdschlüssel: Der Eintrag muss ein gelöschtes Konto überdauern.
    actor_id: int | None = Field(default=None, index=True)
    actor_username: str | None = Field(default=None, sa_column=Column(String(255), index=True))
    # system = vom Scheduler/Sync ausgelöst, nicht von einer Person
    actor_type: str = Field(default="user", sa_column=Column(String(16), nullable=False))

    # Was? Stabile Kennung wie "user.role_changed" — das Frontend übersetzt sie.
    action: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    # Woran? z. B. der betroffene Benutzername oder Einstellungsschlüssel
    target: str | None = Field(default=None, sa_column=Column(String(255)))
    # Ergebnis: success | failure — auch Fehlversuche gehören ins Protokoll
    outcome: str = Field(default="success", sa_column=Column(String(16), nullable=False))

    ip_address: str | None = Field(default=None, sa_column=Column(String(64)))
    user_agent: str | None = Field(default=None, sa_column=Column(String(400)))

    # Kontext ohne Geheimnisse: niemals Passwörter, Tokens oder Secret-Werte ablegen —
    # das Protokoll ist für Admins einsehbar und wird exportiert.
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
