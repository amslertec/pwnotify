"""Mandant (= Kunde). Row-Level-Multi-Tenancy: jede Kundendaten-Zeile trägt tenant_id."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String
from sqlmodel import Field, SQLModel

from ._base import utcnow


class Tenant(SQLModel, table=True):
    __tablename__ = "tenant"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(sa_column=Column(String(200), nullable=False))
    slug: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    # Microsoft-Tenant-ID (tid-Claim) für das SSO-Auto-Mapping; nullable bis SSO konfiguriert ist.
    entra_tenant_id: str | None = Field(
        default=None, sa_column=Column(String(64), unique=True, index=True)
    )
    is_active: bool = Field(default=True)
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class AuditorTenant(SQLModel, table=True):
    """Zuordnung: welcher LOKALE Auditor darf welche Kunden sehen (many-to-many)."""

    __tablename__ = "auditor_tenant"

    user_id: int = Field(foreign_key="app_user.id", primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", primary_key=True)


class AdminTenant(SQLModel, table=True):
    """Zuordnung: welcher LOKALE Admin darf welche Kunden verwalten (many-to-many).

    Pendant zu `AuditorTenant` für Admins unterhalb des Superadmins: der Superadmin
    (role='superadmin') bleibt instanzweit und braucht keine Zeile hier; jeder andere
    lokale Admin (role='admin') wird explizit auf seine(n) Kunden gebunden. FK-Kaskade
    lebt in der Migration (siehe `cd755854e58c`), nicht im Model -- exakt wie bei
    `AuditorTenant`/`a2b3c4d5e6f7`.
    """

    __tablename__ = "admin_tenant"

    user_id: int = Field(foreign_key="app_user.id", primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", primary_key=True)
