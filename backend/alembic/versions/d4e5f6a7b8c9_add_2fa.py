"""add 2FA fields to app_user

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("totp_secret", sa.String(length=255), nullable=True))
    op.add_column(
        "app_user",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("app_user", sa.Column("recovery_codes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "recovery_codes")
    op.drop_column("app_user", "totp_enabled")
    op.drop_column("app_user", "totp_secret")
