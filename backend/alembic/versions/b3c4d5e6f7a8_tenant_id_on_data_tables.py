"""tenant_id on data tables + default tenant + backfill

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Physischer Tabellenname ist `notification_log` (siehe app/models/notification.py).
NOT_NULL_TABLES = ["entra_user", "exclusion", "notification_log", "run"]


def upgrade() -> None:
    # 1. Default-Tenant anlegen und seine id merken.
    conn = op.get_bind()
    tenant_id = conn.execute(
        sa.text(
            "INSERT INTO tenant (name, slug, is_active, created_at) "
            "VALUES ('Meine Firma', 'default', true, now()) RETURNING id"
        )
    ).scalar_one()

    # 2. tenant_id zunächst nullable an alle Datentabellen; bestehende Zeilen backfillen.
    for tbl in [*NOT_NULL_TABLES, "setting", "audit_log"]:
        op.add_column(tbl, sa.Column("tenant_id", sa.Integer(), nullable=True))
        conn.execute(sa.text(f"UPDATE {tbl} SET tenant_id = :tid"), {"tid": tenant_id})

    # 3. Datentabellen (außer audit): NOT NULL + FK + Index.
    # server_default = Phase-1-Brücke: bestehende Writer setzen tenant_id noch nicht
    # explizit; ihre INSERTs fallen so auf den Default-Tenant zurück. In Phase 3, wenn
    # alle Writer tenant_id explizit setzen, wird dieser server_default wieder entfernt.
    for tbl in [*NOT_NULL_TABLES, "setting"]:
        op.alter_column(
            tbl,
            "tenant_id",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text(str(tenant_id)),
        )
        op.create_foreign_key(
            f"fk_{tbl}_tenant", tbl, "tenant", ["tenant_id"], ["id"], ondelete="CASCADE"
        )
        # setting bekommt keinen eigenen Index: der zusammengesetzte PK (tenant_id, key)
        # unten liefert bereits einen tenant_id-führenden Btree; ein separater Index wäre
        # redundant (und das Model hat auch kein index=True auf setting.tenant_id).
        if tbl != "setting":
            op.create_index(f"ix_{tbl}_tenant_id", tbl, ["tenant_id"])

    # audit_log: nullable FK (tenant-lose Ereignisse), SET NULL statt CASCADE.
    op.create_foreign_key(
        "fk_audit_log_tenant", "audit_log", "tenant", ["tenant_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])

    # 4. setting: PK von (key) auf (tenant_id, key) umstellen.
    op.drop_constraint("setting_pkey", "setting", type_="primary")
    op.create_primary_key("setting_pkey", "setting", ["tenant_id", "key"])


def downgrade() -> None:
    op.drop_constraint("setting_pkey", "setting", type_="primary")
    # Setzt voraus, dass pro key höchstens ein Tenant existiert — schlägt fehl, wenn
    # mehrere Tenants sich einen key teilen (Mehrfach-Tenant-Downgrade ist nicht vorgesehen).
    op.create_primary_key("setting_pkey", "setting", ["key"])
    for tbl in ["entra_user", "exclusion", "notification_log", "run", "audit_log"]:
        op.drop_index(f"ix_{tbl}_tenant_id", table_name=tbl)
        op.drop_constraint(f"fk_{tbl}_tenant", tbl, type_="foreignkey")
        op.drop_column(tbl, "tenant_id")
    op.drop_constraint("fk_setting_tenant", "setting", type_="foreignkey")
    op.drop_column("setting", "tenant_id")
    op.execute("DELETE FROM tenant WHERE slug = 'default'")
