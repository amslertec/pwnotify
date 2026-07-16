"""add tenant, auditor_tenant, app_user.tenant_id

Revision ID: a2b3c4d5e6f7
Revises: f6a7b8c9d0e1
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("entra_tenant_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_slug", "tenant", ["slug"], unique=True)
    op.create_index("ix_tenant_entra_tenant_id", "tenant", ["entra_tenant_id"], unique=True)

    op.create_table(
        "auditor_tenant",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "tenant_id"),
    )

    op.add_column("app_user", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_app_user_tenant", "app_user", "tenant", ["tenant_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_app_user_tenant_id", table_name="app_user")
    op.drop_constraint("fk_app_user_tenant", "app_user", type_="foreignkey")
    op.drop_column("app_user", "tenant_id")
    op.drop_table("auditor_tenant")
    op.drop_index("ix_tenant_entra_tenant_id", table_name="tenant")
    op.drop_index("ix_tenant_slug", table_name="tenant")
    op.drop_table("tenant")
