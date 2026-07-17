"""Snapshot der Entra-Gruppenmitglieder einer `AssignmentGroup`, wie zuletzt vom
Gruppen-Sync (Task 4/Graph-Abgleich) gesehen. Eine Zeile pro (Gruppe, Mitglied); `upn`
ist der Match-Key gegen `entra_user`/`app_user`. `synced_at` ist der Zeitstempel des
Snapshots dieser Zeile; `AssignmentGroup.last_synced_at` trägt den Zeitstempel des
zuletzt abgeschlossenen Sync-Laufs für die ganze Gruppe."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from ._base import utcnow


class AssignmentGroupMember(SQLModel, table=True):
    """Ein Mitglied-Snapshot einer Entra-Security-Group. FK-Kaskade auf `assignment_group`
    lebt in der Migration, nicht im Model -- exakt wie bei `AssignmentGroupTenant`: Gruppe
    gelöscht -> ihre Mitglieder-Snapshots verschwinden. Composite-Unique
    `(assignment_group_id, entra_id)` verhindert doppelte Snapshot-Zeilen für dasselbe
    Mitglied derselben Gruppe."""

    __tablename__ = "assignment_group_member"
    __table_args__ = (
        UniqueConstraint(
            "assignment_group_id", "entra_id", name="uq_assignment_group_member_group_entra"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    assignment_group_id: int = Field(foreign_key="assignment_group.id", index=True, nullable=False)
    entra_id: str = Field(sa_column=Column(String(64), index=True, nullable=False))
    upn: str = Field(sa_column=Column(String(320), nullable=False))
    display_name: str | None = Field(default=None, sa_column=Column(String(320), nullable=True))
    mail: str | None = Field(default=None, sa_column=Column(String(320), nullable=True))
    synced_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
