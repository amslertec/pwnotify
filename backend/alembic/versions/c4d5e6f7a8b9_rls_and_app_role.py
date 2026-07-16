"""restricted app role + row-level security

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"
RLS_TABLES = ("entra_user", "exclusion", "notification_log", "run", "setting", "audit_log")
# Fail-safe: ungesetzter GUC liefert '' → NULLIF macht daraus NULL → 0 Zeilen (kein Crash).
_EXPR = "NULLIF(current_setting('app.current_tenant', true), '')::int"


def upgrade() -> None:
    # 1. Eingeschränkte Rolle idempotent anlegen (kein LOGIN/Passwort nötig für SET ROLE).
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{APP_ROLE}') "
        f"THEN CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
    )
    # Attribute unabhängig vom Vorzustand erzwingen: Rollen sind cluster-weit (nicht
    # pro Datenbank), daher könnte eine gleichnamige Rolle bereits mit abweichenden
    # (z. B. LOGIN-fähigen) Attributen existieren. CREATE ROLE ... IF NOT EXISTS
    # überspringt in dem Fall die Attribut-Zuweisung — ALTER ROLE stellt sie sicher her.
    op.execute(f"ALTER ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS")
    # 2. Zugriff auf alle bestehenden + künftigen Tabellen/Sequenzen.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )
    # Laufzeit-Rolle braucht keinen Zugriff auf die Migrations-Buchhaltung (Migrationen laufen
    # als Owner). ALL TABLES oben hat alembic_version mit eingeschlossen -- explizit entziehen.
    op.execute(f"REVOKE ALL ON alembic_version FROM {APP_ROLE}")
    # 3. RLS + Policy pro Tenant-Tabelle. audit_log ist KEIN Sonderfall: tenant-lose Zeilen
    #    (NULL, z. B. Kundenanlage/Auditor-Zuweisung/abgelehnte SSO-Logins) sind gegenüber
    #    jedem restriktiven Tenant-Kontext unsichtbar -- sonst Cross-Tenant-Metadaten-Leak
    #    (Design §3.2). Der lokale Admin sieht sie weiterhin, weil er als Owner läuft und RLS
    #    umgeht; für einen Tenant-Kontext gibt es kein legitimes Publikum für diese Zeilen.
    for tbl in RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {tbl} USING (tenant_id = {_EXPR})")


def downgrade() -> None:
    for tbl in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
