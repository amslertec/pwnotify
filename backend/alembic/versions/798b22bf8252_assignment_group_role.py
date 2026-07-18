"""assignment_group role

Revision ID: 798b22bf8252
Revises: 8573de47a2a1
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "798b22bf8252"
down_revision: str | None = "8573de47a2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills every existing row to "admin" (today's implicit behaviour:
    # the user's global role). Kept on the column afterwards as a safety net for any future
    # insert path that omits the field.
    op.add_column(
        "assignment_group",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="admin"),
    )


def downgrade() -> None:
    op.drop_column("assignment_group", "role")
