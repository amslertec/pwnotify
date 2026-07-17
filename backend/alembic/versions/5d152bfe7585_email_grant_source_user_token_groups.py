"""email, grant source, invitation, assignment groups

Revision ID: 5d152bfe7585
Revises: 4035552093e2
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d152bfe7585"
down_revision: str | None = "4035552093e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. app_user.email -- nullable, kein Unique-Constraint (SSO-UPN lebt in `username`,
    #    eine Person kann sowohl ein SSO- als auch ein lokales Konto haben).
    op.add_column("app_user", sa.Column("email", sa.String(320), nullable=True))

    # 2. `source` auf beiden Grant-Tabellen -- server_default backfillt bestehende Zeilen
    #    auf 'manual' (sie wurden alle von Hand vergeben, es gab noch keine Gruppen).
    op.add_column(
        "admin_tenant",
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "auditor_tenant",
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
    )

    # 3. Assignment-Groups (Entra-Gruppen -> Kunden-Mapping).
    op.create_table(
        "assignment_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("entra_group_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assignment_group_entra_group_id"),
        "assignment_group",
        ["entra_group_id"],
        unique=True,
    )
    op.create_table(
        "assignment_group_tenant",
        sa.Column("assignment_group_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_group_id"], ["assignment_group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assignment_group_id", "tenant_id"),
    )

    # 4. user_token -- generalisiertes Einmal-Token (invite + reset), siehe Docstring in
    #    app/models/token.py. FK auf app_user_id kaskadiert (Konto weg -> Tokens weg);
    #    created_by (der Ersteller/Admin) kaskadiert bewusst NICHT -- ein gelöschtes
    #    Erstellerkonto darf ein noch gültiges Token eines anderen Nutzers nicht mitreissen.
    op.create_table(
        "user_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_token_app_user_id"), "user_token", ["app_user_id"], unique=False)
    op.create_index(op.f("ix_user_token_token_hash"), "user_token", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_token_token_hash"), table_name="user_token")
    op.drop_index(op.f("ix_user_token_app_user_id"), table_name="user_token")
    op.drop_table("user_token")

    op.drop_table("assignment_group_tenant")
    op.drop_index(op.f("ix_assignment_group_entra_group_id"), table_name="assignment_group")
    op.drop_table("assignment_group")

    op.drop_column("auditor_tenant", "source")
    op.drop_column("admin_tenant", "source")

    op.drop_column("app_user", "email")
