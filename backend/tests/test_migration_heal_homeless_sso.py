"""Verifiziert Migration `38535d051a81` (homeless SSO -> Default-Tenant) direkt.

Wie `test_migration_is_default_home.py`: die savepoint-isolierte `session`-Fixture reicht
nicht -- der UPDATE läuft nur EINMAL, beim `upgrade()` selbst, und `migrated_engine` hat
die Migration bereits vor jedem Test gegen eine leere DB gefahren. Dieser Test treibt die
Migration daher selbst: downgrade auf den Vorgänger-Head, Testdaten committen, upgrade
zurück auf `head`, Ergebnis prüfen.

Läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) -- der
`finally`-Block räumt deshalb explizit auf: selbst angelegte Test-Zeilen löschen, wieder
upgrade auf `head` -- damit nachfolgende Tests im selben Lauf (derselbe physische
Testcontainer, Port 5433) eine unveränderte, rückstandsfreie DB vorfinden.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PREV_REVISION = "b1c2d3e4f5a6"
THIS_REVISION = "38535d051a81"

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return cfg


async def _downgrade(revision: str) -> None:
    # env.py läuft intern über asyncio.run() -- darf nicht in einem bereits aktiven Loop
    # laufen (siehe app/db/migrate.py::run_migrations); pytest-asyncio hat aber selbst
    # einen Loop offen, deshalb in einen Thread auslagern (gleiches Muster wie conftest.py).
    await asyncio.to_thread(command.downgrade, _alembic_config(), revision)


async def _upgrade(revision: str = "head") -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _uname(tag: str) -> str:
    return f"mig3-{tag}-{uuid.uuid4().hex[:8]}"


async def test_homeless_sso_accounts_healed_to_default_tenant(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_users: list[str] = []
    default_tenant_id: int | None = None
    customer_tenant_id: int | None = None
    customer_slug = f"mig3-customer-{uuid.uuid4().hex[:8]}"

    try:
        # 1. Auf den Vorgänger-Head zurück: der UPDATE dieser Migration hat noch nicht
        #    gelaufen -- der Zustand vor dieser Migration (Prod-Altlast reproduziert).
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            default_tenant_id = (
                await conn.execute(text("SELECT id FROM tenant WHERE is_default"))
            ).scalar_one()
            customer_tenant_id = (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES ('Mig3 Customer', :slug, true, now()) RETURNING id"
                    ),
                    {"slug": customer_slug},
                )
            ).scalar_one()

            homeless_sso = _uname("homeless-sso")
            homed_sso = _uname("homed-sso")
            homeless_local = _uname("homeless-local")
            seeded_users += [homeless_sso, homed_sso, homeless_local]

            async def _mk_user(username: str, *, is_sso: bool, tenant_id: int | None) -> int:
                return (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, tenant_id, failed_login_count, created_at, updated_at) "
                            "VALUES (:u, 'x', 'admin', true, :is_sso, :tid, 0, now(), now()) "
                            "RETURNING id"
                        ),
                        {"u": username, "is_sso": is_sso, "tid": tenant_id},
                    )
                ).scalar_one()

            # Genau der Prod-Befund: SSO-Konto ohne Heimat -- muss geheilt werden.
            homeless_sso_id = await _mk_user(homeless_sso, is_sso=True, tenant_id=None)
            # SSO-Konto MIT Heimat (Kundentenant) -- muss unangetastet bleiben.
            homed_sso_id = await _mk_user(homed_sso, is_sso=True, tenant_id=customer_tenant_id)
            # NICHT-SSO-Konto ohne Heimat -- fällt nicht unter diese Migration, muss
            # unangetastet bleiben (NULL bleibt NULL).
            homeless_local_id = await _mk_user(homeless_local, is_sso=False, tenant_id=None)

        # 2. Die Migration selbst treiben: der Heal-UPDATE passiert genau hier.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            homeless_sso_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"),
                    {"id": homeless_sso_id},
                )
            ).scalar_one()
            assert homeless_sso_tid == default_tenant_id, (
                "homeloses SSO-Konto wurde nicht auf den Default-Tenant geheilt"
            )

            homed_sso_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"), {"id": homed_sso_id}
                )
            ).scalar_one()
            assert homed_sso_tid == customer_tenant_id, (
                "bereits geheimatetes SSO-Konto darf sich nicht ändern"
            )

            homeless_local_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"),
                    {"id": homeless_local_id},
                )
            ).scalar_one()
            assert homeless_local_tid is None, (
                "NICHT-SSO-Konto ohne Heimat darf von dieser Migration nicht angefasst werden"
            )
    finally:
        # Aufräumen: Test-Zeilen löschen, dann wieder upgrade auf `head` -- damit
        # nachfolgende Tests im selben Lauf eine unveränderte, rückstandsfreie DB vorfinden.
        # (Kein Downgrade nötig: `downgrade()` dieser Migration ist ein bewusster No-Op.)
        await _upgrade("head")
        async with migrated_engine.begin() as conn:
            if seeded_users:
                await conn.execute(
                    text("DELETE FROM app_user WHERE username = ANY(:names)"),
                    {"names": seeded_users},
                )
            await conn.execute(
                text("DELETE FROM tenant WHERE slug = :slug"), {"slug": customer_slug}
            )
