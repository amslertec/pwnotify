"""add app_user.totp_last_step (TOTP-Replay-Schutz)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: bestehende Konten haben noch keinen verbrauchten Schritt.
    op.add_column("app_user", sa.Column("totp_last_step", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "totp_last_step")
