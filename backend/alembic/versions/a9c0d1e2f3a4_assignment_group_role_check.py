"""CHECK constraint on assignment_group.role -- DB-side role allow-list (L7)

Revision ID: a9c0d1e2f3a4
Revises: f8a9b0c1d2e3
Create Date: 2026-07-20

`798b22bf8252` added `assignment_group.role` as `String(16)` NOT NULL with a
`server_default='admin'` but no value constraint, so the database accepts any string.
The application layer already narrows the field to `Literal["admin", "auditor"]` and
degrades foreign values fail-safe, yet nothing stops a raw SQL path (or a future bug)
from persisting a bogus role. Defense-in-depth: this CHECK constraint enforces the same
allow-list at the storage layer.

Existing rows can only ever be 'admin'/'auditor' -- the app has written nothing else and
the backfill default was 'admin' -- so ADD CONSTRAINT validates without a data cleanup.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a9c0d1e2f3a4"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_assignment_group_role"


def upgrade() -> None:
    # Mirror the app-layer allow-list (Literal["admin", "auditor"]) at the DB level.
    op.create_check_constraint(
        _CONSTRAINT,
        "assignment_group",
        "role IN ('admin', 'auditor')",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "assignment_group", type_="check")
