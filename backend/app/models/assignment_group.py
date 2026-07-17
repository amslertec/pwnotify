"""Entra-Gruppen, deren Mitglieder automatisch Zugriff auf zugeordnete Kunden erhalten."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String
from sqlmodel import Field, SQLModel

from ._base import utcnow


class AssignmentGroup(SQLModel, table=True):
    """Eine Entra-Security-Group des PROVIDER-Tenants, die auf einen oder mehrere Kunden
    gemappt ist (many-to-many über `AssignmentGroupTenant`). Der Gruppen-Reconcile (Task 4)
    gleicht Mitgliedschaften gegen `admin_tenant`/`auditor_tenant`-Zeilen mit `source='group'`
    ab; eine bereits vorhandene `source='manual'`-Zeile bleibt davon unberührt (siehe
    Docstring auf `AdminTenant`/`AuditorTenant`)."""

    __tablename__ = "assignment_group"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(sa_column=Column(String(200), nullable=False))
    entra_group_id: str = Field(
        sa_column=Column(String(64), unique=True, index=True, nullable=False)
    )
    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class AssignmentGroupTenant(SQLModel, table=True):
    """Zuordnung: welche Assignment-Group gewährt Zugriff auf welchen Kunden (many-to-many).
    FK-Kaskade in beide Richtungen lebt in der Migration, nicht im Model -- exakt wie bei
    `AdminTenant`/`AuditorTenant`: Gruppe gelöscht -> ihre Zuordnungen verschwinden; Kunde
    gelöscht -> seine Mitgliedschaftszeilen verschwinden."""

    __tablename__ = "assignment_group_tenant"

    assignment_group_id: int = Field(foreign_key="assignment_group.id", primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", primary_key=True)
