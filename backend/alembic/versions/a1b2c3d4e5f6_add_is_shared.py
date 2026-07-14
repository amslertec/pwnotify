"""add is_shared to entra_user

Revision ID: a1b2c3d4e5f6
Revises: c92a9a84bc1e
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "c92a9a84bc1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entra_user",
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("entra_user", "is_shared")
