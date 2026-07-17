"""user_session.active_tenant_id -- durable Ablage des aktiven Mandanten (Phase 4a, Task 1)

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_session", sa.Column("active_tenant_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_user_session_active_tenant",
        "user_session",
        "tenant",
        ["active_tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_user_session_active_tenant_id", "user_session", ["active_tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_user_session_active_tenant_id", table_name="user_session")
    op.drop_constraint("fk_user_session_active_tenant", "user_session", type_="foreignkey")
    op.drop_column("user_session", "active_tenant_id")
