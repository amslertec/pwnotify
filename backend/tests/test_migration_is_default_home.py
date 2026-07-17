"""Verifiziert Migration `4035552093e2` (tenant.is_default + Heim-Tenant-Backfill) direkt.

Wie `test_migration_access_model.py`: die savepoint-isolierte `session`-Fixture reicht
nicht -- die Datentransformation (Incumbent markieren, lokale Konten ohne Heimat
backfillen) läuft nur EINMAL, beim `upgrade()` selbst, und `migrated_engine` hat die
Migration bereits vor jedem Test gegen eine leere DB gefahren. Dieser Test treibt die
Migration daher selbst: downgrade auf den Vorgänger-Head, Testdaten committen, upgrade
zurück auf `THIS_REVISION`, Ergebnis prüfen.

Läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) -- der
`finally`-Block räumt deshalb explizit auf: Slug-Rename zurücknehmen, downgrade (entfernt
`is_default`/den Index, setzt lokale Konten wieder auf NULL-Heimat), selbst angelegte
Test-Zeilen löschen, wieder upgrade auf `head` -- damit nachfolgende Tests im selben Lauf
(derselbe physische Testcontainer, Port 5433) eine unveränderte, rückstandsfreie DB
vorfinden. Voller Testlauf zweimal hintereinander grün bestätigt das (siehe Task-Report).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.repositories import tenant_repo
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

PREV_REVISION = "cd755854e58c"
THIS_REVISION = "4035552093e2"

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
    return f"mig2-{tag}-{uuid.uuid4().hex[:8]}"


async def test_is_default_backfill_unique_guard_and_rename_safe_lookup(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_users: list[str] = []
    seeded_tenants: list[str] = []
    default_tenant_id: int | None = None

    try:
        # 1. Auf den Vorgänger-Head zurück: `is_default` existiert noch nicht, lokale Konten
        #    ohne eigene Zuordnung tragen (noch) NULL in `tenant_id` -- der Zustand vor
        #    dieser Migration.
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            default_tenant_id = (
                await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
            ).scalar_one()

            customer_slug = f"mig2-customer-{uuid.uuid4().hex[:8]}"
            customer_tenant_id = (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES ('Mig2 Customer', :slug, true, now()) RETURNING id"
                    ),
                    {"slug": customer_slug},
                )
            ).scalar_one()
            seeded_tenants.append(customer_slug)

            local_admin = _uname("local-admin")
            sso_user = _uname("sso-user")
            legacy_customer_admin = _uname("legacy-customer-admin")
            seeded_users += [local_admin, sso_user, legacy_customer_admin]

            async def _mk_user(
                username: str, *, role: str, is_sso: bool = False, tenant_id: int | None = None
            ) -> int:
                return (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, tenant_id, failed_login_count, created_at, updated_at) "
                            "VALUES (:u, 'x', :role, true, :is_sso, :tid, 0, now(), now()) "
                            "RETURNING id"
                        ),
                        {"u": username, "role": role, "is_sso": is_sso, "tid": tenant_id},
                    )
                ).scalar_one()

            # Pre-existing lokaler Admin ohne eigene Zuordnung -- genau der Fall, den die
            # Migration provider-heimaten muss.
            local_admin_id = await _mk_user(local_admin, role="admin", tenant_id=None)
            # SSO-Konto, bereits homed im Kundentenant -- muss unangetastet bleiben.
            sso_user_id = await _mk_user(
                sso_user, role="admin", is_sso=True, tenant_id=customer_tenant_id
            )
            # Legacy-Kundenstaff (Finding 1, Whole-Branch-Review): ein VOR Task 3 angelegter
            # lokaler Admin -- `tenant_id IS NULL` (die Heim-Tenant-Spalte gab es damals noch
            # nicht), aber bereits mit einer `admin_tenant`-Zuweisung auf den (Nicht-Default-)
            # Kundentenant. Ohne die Backfill-Ausnahme wäre dieses Konto fälschlich auf den
            # Default-Tenant provider-geheimatet worden -- und damit über
            # `tenant_repo.is_provider_account` fälschlich cross-grantbar geworden.
            legacy_customer_admin_id = await _mk_user(
                legacy_customer_admin, role="admin", tenant_id=None
            )
            await conn.execute(
                text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:uid, :tid)"),
                {"uid": legacy_customer_admin_id, "tid": customer_tenant_id},
            )

        # 2. Die Migration selbst treiben: Incumbent markieren + Backfill passiert genau hier.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            # -- exakt EIN Tenant mit is_default=true, und zwar der Incumbent.
            default_rows = (
                (await conn.execute(text("SELECT id FROM tenant WHERE is_default"))).scalars().all()
            )
            assert default_rows == [default_tenant_id]

            # -- lokaler Admin ist jetzt provider-geheimatet (home = Default-Tenant).
            local_admin_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"), {"id": local_admin_id}
                )
            ).scalar_one()
            assert local_admin_tid == default_tenant_id, "lokaler Admin wurde nicht backfilled"

            # -- SSO-Konto unangetastet, weiterhin im Kundentenant.
            sso_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"), {"id": sso_user_id}
                )
            ).scalar_one()
            assert sso_tid == customer_tenant_id, "SSO-Konto tenant_id darf sich nicht ändern"

            # -- Legacy-Kundenstaff (Finding 1): NICHT provider-geheimatet, obwohl
            #    `tenant_id IS NULL` -- die bestehende `admin_tenant`-Zuweisung auf einen
            #    Nicht-Default-Tenant nimmt das Konto von der Provider-Promotion aus. Home
            #    bleibt NULL (die Zuweisung selbst gibt bereits Zugriff; NULL-Heimat ->
            #    `is_provider_account`=False, also strukturell nicht cross-grantbar).
            legacy_customer_admin_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"),
                    {"id": legacy_customer_admin_id},
                )
            ).scalar_one()
            assert legacy_customer_admin_tid is None, (
                "Legacy-Kundenstaff mit admin_tenant-Zuweisung darf NICHT auf den "
                "Default-Tenant provider-geheimatet werden"
            )

        # -- partieller Unique-Index: ein zweiter is_default=true auf dem Kundentenant
        #    muss scheitern (höchstens ein Default gleichzeitig möglich).
        async with migrated_engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text("UPDATE tenant SET is_default = true WHERE id = :id"),
                    {"id": customer_tenant_id},
                )
            await trans.rollback()

        # -- `tenant_repo.default_tenant()` löst den Incumbent auch nach einer Slug-
        #    Umbenennung weiterhin über `is_default` auf, nicht mehr über den (jetzt
        #    geänderten) Slug.
        renamed_slug = f"mig2-renamed-{uuid.uuid4().hex[:8]}"
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tenant SET slug = :slug WHERE id = :id"),
                {"slug": renamed_slug, "id": default_tenant_id},
            )

        async with async_sessionmaker(bind=migrated_engine, expire_on_commit=False)() as s:
            resolved = await tenant_repo.default_tenant(s)
            assert resolved.id == default_tenant_id
            assert resolved.slug == renamed_slug
    finally:
        # Slug-Rename zuerst zurücknehmen -- andere Tests im selben Lauf verlassen sich auf
        # `slug = 'default'` für den Incumbent (siehe z.B. test_tenant_authorization.py).
        if default_tenant_id is not None:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE tenant SET slug = 'default' WHERE id = :id"),
                    {"id": default_tenant_id},
                )
        # Downgrade: entfernt Backfill (lokale Konten zurück auf NULL), Index, Spalte.
        await _downgrade(PREV_REVISION)
        async with migrated_engine.begin() as conn:
            if seeded_users:
                await conn.execute(
                    text("DELETE FROM app_user WHERE username = ANY(:names)"),
                    {"names": seeded_users},
                )
            if seeded_tenants:
                await conn.execute(
                    text("DELETE FROM tenant WHERE slug = ANY(:slugs)"),
                    {"slugs": seeded_tenants},
                )
        await _upgrade("head")
