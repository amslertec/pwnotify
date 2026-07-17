"""entra_id mandantensicher -- unique nur pro (tenant_id, entra_id)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bisher: entra_id global unique (ix_entra_user_entra_id) -- das ist ein Cross-Tenant-
    # Existence-Oracle (ein Tenant könnte per Duplicate-Key-Fehler erraten, ob ein entra_id in
    # einem ANDEREN Tenant existiert) und blockiert, denselben Entra-User in zwei Kunden zu
    # spiegeln. Ersetzt durch einen Unique-Constraint auf (tenant_id, entra_id); entra_id
    # behält einen nicht-unique Index für Lookups.
    op.drop_index("ix_entra_user_entra_id", table_name="entra_user")
    op.create_index("ix_entra_user_entra_id", "entra_user", ["entra_id"])
    op.create_unique_constraint("uq_entra_tenant_entra_id", "entra_user", ["tenant_id", "entra_id"])


def downgrade() -> None:
    op.drop_constraint("uq_entra_tenant_entra_id", "entra_user", type_="unique")
    op.drop_index("ix_entra_user_entra_id", table_name="entra_user")
    op.create_index("ix_entra_user_entra_id", "entra_user", ["entra_id"], unique=True)
