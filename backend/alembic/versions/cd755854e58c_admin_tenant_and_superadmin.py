"""admin_tenant and superadmin

Revision ID: cd755854e58c
Revises: a8b9c0d1e2f3
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd755854e58c"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"


def upgrade() -> None:
    op.create_table(
        "admin_tenant",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "tenant_id"),
    )

    # Instanzweite Tabelle wie `auditor_tenant` -- least privilege wie in `f7a8b9c0d1e2`:
    # eine frisch angelegte Tabelle sonst mit dem Phase-2-Blanket-Grant (`GRANT ... ON ALL
    # TABLES IN SCHEMA public`) stehen zu lassen wäre die einzige Lücke, die dieses Task
    # selbst aufreissen würde. SELECT bleibt (ein künftiger Tenant-Switcher könnte lesen
    # müssen), INSERT/UPDATE/DELETE laufen ausschliesslich über die Owner-Session.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON admin_tenant FROM {APP_ROLE}")

    conn = op.get_bind()

    # Ältester lokaler Admin (erstes Setup-Konto) wird Superadmin -- instanzweit, ausserhalb
    # jeder Mandantengrenze. Genau ein Konto muss diese Grenze überhaupt noch überschreiten
    # dürfen (Tenant-Verwaltung, Instanz-Settings); alle anderen bestehenden Admins werden
    # unten explizit auf den Default-Kunden gebunden statt implizit instanzweit zu bleiben.
    conn.execute(
        sa.text(
            "UPDATE app_user SET role = 'superadmin' "
            "WHERE id = (SELECT id FROM app_user WHERE is_sso = false AND role = 'admin' "
            "ORDER BY id ASC LIMIT 1)"
        )
    )

    # Alle übrigen lokalen Admins bleiben role='admin', erhalten aber eine explizite
    # admin_tenant-Zuordnung auf den Default-Kunden. Der eben beförderte Superadmin ist
    # bereits ausgeschlossen -- sein role ist nach der UPDATE oben nicht mehr 'admin'.
    # Auditoren behalten ihre auditor_tenant-Zuordnungen, SSO-Konten ihre tenant_id --
    # beide unverändert.
    conn.execute(
        sa.text(
            "INSERT INTO admin_tenant (user_id, tenant_id) "
            "SELECT u.id, t.id FROM app_user u CROSS JOIN tenant t "
            "WHERE u.is_sso = false AND u.role = 'admin' AND t.slug = 'default'"
        )
    )

    # Mandantenfähigkeit-Modus-Flag seeden, standardmässig AUS. Speicherort (Default-Tenant)
    # und der gated Write dazu sind Task 5 -- hier nur die registrierte Einstellung mit
    # ihrem Default-Wert, damit `SettingsService.get_all()` den Key von Anfang an kennt.
    # `value` ist JSONB (siehe app/models/setting.py) -- `false` als JSON-Bool, nicht als
    # Text, sonst würde `SettingsService.get_all()` beim Auslesen einen String zurückgeben.
    conn.execute(
        sa.text(
            "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
            "SELECT id, 'instance.multi_tenant_mode', 'false'::jsonb, false, now() "
            "FROM tenant WHERE slug = 'default' "
            "ON CONFLICT (tenant_id, key) DO NOTHING"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Seed-Setting entfernen.
    conn.execute(
        sa.text(
            "DELETE FROM setting WHERE key = 'instance.multi_tenant_mode' "
            "AND tenant_id = (SELECT id FROM tenant WHERE slug = 'default')"
        )
    )

    # Superadmin zurückstufen. Best-effort-symmetrisch zu upgrade(): trifft jedes Konto mit
    # role='superadmin' zurück auf 'admin' -- korrekt, solange dieser Migrationspfad die
    # einzige Quelle der Rolle ist (kein anderer Code-Pfad vergibt sie bisher). Ein
    # zwischenzeitlich manuell umbenanntes zweites Superadmin-Konto würde hier ebenfalls
    # zurückgestuft; das ist im Rahmen dieses Downgrades hinnehmbar.
    conn.execute(sa.text("UPDATE app_user SET role = 'admin' WHERE role = 'superadmin'"))

    # Grant wiederherstellen (Phase-2-Blanket-Zustand, wie im downgrade-Muster von
    # `f7a8b9c0d1e2`) -- der Vollständigkeit halber, auch wenn die Tabelle gleich mitsamt
    # ihrer Grants verschwindet.
    op.execute(f"GRANT INSERT, UPDATE, DELETE ON admin_tenant TO {APP_ROLE}")
    op.drop_table("admin_tenant")
