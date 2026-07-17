"""drop tenant_id server_default (Phase-1-Brücke) -- kontext-abhängiger Default statt statisch

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Physischer Tabellenname ist `notification_log` (siehe app/models/notification.py).
NOT_NULL_TENANT_TABLES = ("entra_user", "exclusion", "notification_log", "run", "setting")


def upgrade() -> None:
    # Phase 1 gab diesen fünf NOT-NULL-Spalten einen statischen server_default (die
    # Default-Tenant-id), damit bestehende Writer ohne explizites tenant_id weiterliefen.
    # Phase 3 stempelt tenant_id stattdessen kontext-abhängig im ORM (`default_factory=
    # current_tenant_or_none`, siehe app/models/_base.py) -- der DB-server_default entfällt,
    # damit ein INSERT ohne aktiven Tenant-Kontext NICHT mehr still auf den Default-Tenant
    # zurückfällt, sondern mit NOT NULL fehlschlägt (gewollter, sichtbarer Fehler statt
    # stillem Daten-Leck in den falschen Tenant).
    for tbl in NOT_NULL_TENANT_TABLES:
        op.alter_column(tbl, "tenant_id", server_default=None)


def downgrade() -> None:
    # Default-Tenant-ID als server_default wiederherstellen (Rückweg zur Phase-1-Brücke).
    tid = op.get_bind().execute(sa.text("SELECT id FROM tenant WHERE slug='default'")).scalar_one()
    for tbl in NOT_NULL_TENANT_TABLES:
        op.alter_column(tbl, "tenant_id", server_default=sa.text(str(tid)))
