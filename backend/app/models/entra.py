"""Gespiegelte Entra-ID-Benutzer + Ausschluss-Regeln."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..db.tenant_context import current_tenant_or_none
from ._base import utcnow


class EntraUser(SQLModel, table=True):
    __tablename__ = "entra_user"
    # entra_id ist NICHT global unique: derselbe Entra-User kann in zwei Kunden gespiegelt
    # werden. Ein globaler Unique-Constraint wäre außerdem ein Cross-Tenant-Existence-Oracle
    # (ein Tenant könnte per Duplicate-Key-Fehler erraten, ob ein entra_id in einem ANDEREN
    # Tenant existiert). Stattdessen: Unique pro (tenant_id, entra_id).
    __table_args__ = (UniqueConstraint("tenant_id", "entra_id", name="uq_entra_tenant_entra_id"),)

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(
        foreign_key="tenant.id",
        index=True,
        nullable=False,
        default_factory=current_tenant_or_none,
    )
    entra_id: str = Field(sa_column=Column(String(64), index=True, nullable=False))
    upn: str = Field(sa_column=Column(String(320), index=True, nullable=False))
    display_name: str = Field(default="", sa_column=Column(String(320), nullable=False))
    mail: str | None = Field(default=None, sa_column=Column(String(320)))
    other_mails: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    account_enabled: bool = Field(default=True)

    department: str | None = Field(default=None, sa_column=Column(String(200)))
    job_title: str | None = Field(default=None, sa_column=Column(String(200)))
    language: str | None = Field(default=None, sa_column=Column(String(8)))

    last_password_change: dt.datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    password_policies: str | None = Field(default=None, sa_column=Column(String(200)))

    # Abgeleitete Ablauf-Felder (bei jedem Sync neu berechnet)
    password_never_expires: bool = Field(default=False)
    expiry_date: dt.datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    days_left: int | None = Field(default=None)

    excluded: bool = Field(default=False)
    # Als Shared Mailbox erkannt (Muster) -> aus Benutzerliste ausgeblendet, keine Reminder.
    is_shared: bool = Field(default=False)

    raw: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    last_synced_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class Exclusion(SQLModel, table=True):
    """Ausschluss von Benachrichtigungen für einen User oder eine ganze Gruppe."""

    __tablename__ = "exclusion"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(
        foreign_key="tenant.id",
        index=True,
        nullable=False,
        default_factory=current_tenant_or_none,
    )
    kind: str = Field(sa_column=Column(String(16), nullable=False))  # "user" | "group"
    value: str = Field(sa_column=Column(String(320), nullable=False))  # entra_id / group-id / upn
    label: str | None = Field(default=None, sa_column=Column(String(320)))
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
