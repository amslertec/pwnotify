"""assignment_group_member snapshot + last_synced_at

Revision ID: 8573de47a2a1
Revises: 5d152bfe7585
Create Date: 2026-07-17 22:40:11.622914
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8573de47a2a1"
down_revision: str | None = "5d152bfe7585"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `last_synced_at` -- Zeitstempel des zuletzt abgeschlossenen Gruppen-Sync-Laufs.
    # NULL = noch nie synchronisiert (bestehende Zeilen bleiben unangetastet).
    op.add_column(
        "assignment_group", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Mitglieder-Snapshot je Gruppe: eine Zeile pro (Gruppe, Entra-Mitglied). FK kaskadiert
    # wie `assignment_group_tenant` (Gruppe gelöscht -> ihre Snapshot-Zeilen verschwinden).
    op.create_table(
        "assignment_group_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("assignment_group_id", sa.Integer(), nullable=False),
        sa.Column("entra_id", sa.String(length=64), nullable=False),
        sa.Column("upn", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=320), nullable=True),
        sa.Column("mail", sa.String(length=320), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_group_id"], ["assignment_group.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_group_id",
            "entra_id",
            name="uq_assignment_group_member_group_entra",
        ),
    )
    op.create_index(
        op.f("ix_assignment_group_member_assignment_group_id"),
        "assignment_group_member",
        ["assignment_group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assignment_group_member_entra_id"),
        "assignment_group_member",
        ["entra_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_assignment_group_member_entra_id"), table_name="assignment_group_member")
    op.drop_index(
        op.f("ix_assignment_group_member_assignment_group_id"),
        table_name="assignment_group_member",
    )
    op.drop_table("assignment_group_member")

    op.drop_column("assignment_group", "last_synced_at")
