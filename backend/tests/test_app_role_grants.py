"""Least-Privilege-Beweis: `pwnotify_app` verliert Zugriff auf instanzweite Tabellen.

Tasks 3/4 haben belegt, dass die tenant-gescopte Rolle `app_user`/`tenant`/`user_session`/
`auditor_tenant` nie anfasst -- jeder Zugriff läuft auf der Owner-Session. Diese Tests
prüfen die tatsächlich erteilten Postgres-Grants (nicht das ORM-Verhalten) via
`has_table_privilege`, damit ein künftiges pauschales Re-GRANT sofort auffällt.
"""

from __future__ import annotations

from app.db.rls import APP_ROLE, RLS_TABLES
from sqlalchemy import text

INSTANCE_WIDE_TABLES = ("tenant", "app_user", "user_session", "auditor_tenant")

INSTANCE_WIDE_NO_ACCESS = (
    "user_token",
    "assignment_group",
    "assignment_group_tenant",
    "assignment_group_member",
)


async def _priv(session, table: str, priv: str) -> bool:
    return (
        await session.execute(
            text("SELECT has_table_privilege(:role, :table, :priv)"),
            {"role": APP_ROLE, "table": table, "priv": priv},
        )
    ).scalar_one()


async def test_no_write_privileges_on_instance_wide_tables(session):
    for tbl in INSTANCE_WIDE_TABLES:
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not await _priv(session, tbl, priv), (
                f"{APP_ROLE} kann noch {priv} auf {tbl} -- Least-Privilege verletzt"
            )


async def test_no_select_on_app_user_and_user_session(session):
    # Passwort-Hashes / Refresh-Token-Material -- kein tenant-gescopter Lesepfad existiert.
    for tbl in ("app_user", "user_session"):
        assert not await _priv(session, tbl, "SELECT"), (
            f"{APP_ROLE} kann noch SELECT auf {tbl} lesen -- Secrets exponiert"
        )


async def test_select_still_allowed_on_tenant_and_auditor_tenant(session):
    # Bewusst behalten: keine Geheimnisse, ein künftiger Tenant-Switcher könnte lesen müssen.
    for tbl in ("tenant", "auditor_tenant"):
        assert await _priv(session, tbl, "SELECT"), f"{APP_ROLE} sollte {tbl} noch lesen dürfen"


async def test_app_role_has_no_privileges_on_provider_tables(session):
    # These four instance-wide tables are never touched by the tenant-scoped role; a
    # compromised pwnotify_app path must not reach token hashes, member PII, or group config.
    for tbl in INSTANCE_WIDE_NO_ACCESS:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await _priv(session, tbl, priv), (
                f"{APP_ROLE} still has {priv} on {tbl} -- least-privilege violated"
            )


async def test_rls_tables_keep_full_crud(session):
    # Der eigentliche Betrieb darf nicht kaputtgehen: die sechs RLS-Tabellen brauchen weiter
    # das volle CRUD, das Phase 2 erteilt hat.
    for tbl in RLS_TABLES:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await _priv(session, tbl, priv), f"{APP_ROLE} braucht {priv} auf {tbl}"
