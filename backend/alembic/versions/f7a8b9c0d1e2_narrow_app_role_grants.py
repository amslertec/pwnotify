"""narrow app role grants -- least privilege on instance-wide tables

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"

# Instanzweite Tabellen (kein tenant_id, kein RLS-Backstop). Tasks 3/4 haben empirisch belegt,
# dass die tenant-gescopte `pwnotify_app`-Rolle KEINEN dieser vier Tabellen anfasst -- jeder
# Zugriff (Login/Session, `default_tenant_id`-Lookup, SSO-Abgleich, Seed, Auditor-Zuweisung)
# läuft auf der Owner-Session (`pwnotify`, kein `use_tenant`-Block aktiv, siehe
# `app/api/deps.py::get_session`/`SessionDep`, `app/db/tenant_context.py::use_owner_context`).
# Phase 2 (`c4d5e6f7a8b9`) hat der Rolle aber pauschal volles CRUD via
# `GRANT ... ON ALL TABLES IN SCHEMA public` erteilt -- ohne RLS-Policy auf diesen vier
# Tabellen wäre ein kompromittierter `pwnotify_app`-Pfad (z. B. SQL-Injection in einer
# tenant-gescopten Route) sonst frei, Passwort-Hashes/Sessions zu lesen oder Mandanten zu
# löschen. Least Privilege: nur entziehen, was nachweislich ungenutzt ist.
WRITE_ONLY_TABLES = ("tenant", "app_user", "user_session", "auditor_tenant")

# Zusätzlich SELECT entziehen: keine Rolle im tenant-gescopten Pfad liest Passwort-Hashes
# (`app_user`) oder Refresh-Token-Material (`user_session`) -- Auth läuft komplett auf der
# Owner-Session. `tenant`/`auditor_tenant` behalten SELECT: ein künftiger Tenant-Switcher
# könnte sie lesen müssen, und sie enthalten keine Geheimnisse (Name/Slug/Zuordnung).
SELECT_ALSO_REVOKED = ("app_user", "user_session")


def upgrade() -> None:
    for tbl in WRITE_ONLY_TABLES:
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON {tbl} FROM {APP_ROLE}")
    for tbl in SELECT_ALSO_REVOKED:
        op.execute(f"REVOKE SELECT ON {tbl} FROM {APP_ROLE}")


def downgrade() -> None:
    # Phase-2-Blanket-Zustand für diese vier Tabellen wiederherstellen (tabellenspezifisch,
    # nicht über `ALL TABLES IN SCHEMA public` -- die Rolle bleibt sonst unverändert).
    for tbl in SELECT_ALSO_REVOKED:
        op.execute(f"GRANT SELECT ON {tbl} TO {APP_ROLE}")
    for tbl in WRITE_ONLY_TABLES:
        op.execute(f"GRANT INSERT, UPDATE, DELETE ON {tbl} TO {APP_ROLE}")
