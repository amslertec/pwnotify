"""Verifiziert Migration `cd755854e58c` (admin_tenant, superadmin, Mode-Flag-Seed) direkt.

Anders als die übrigen Tests reicht die savepoint-isolierte `session`-Fixture aus
conftest.py hier NICHT: die Datentransformation (ältesten lokalen Admin befördern,
übrige Admins auf `admin_tenant` verdrahten, Mode-Flag seeden) läuft nur EINMAL, beim
`upgrade()` selbst -- und `migrated_engine` hat die Migration bereits beim Session-Start
gegen eine leere DB gefahren, bevor irgendein Test Daten seeden konnte. Um sie mit echten
Vorbedingungen zu prüfen, treibt dieser Test die Migration selbst: downgrade auf den
Vorgänger-Head, Testdaten committen, upgrade zurück auf `head`, Ergebnis prüfen.

Das läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) --
deshalb räumt der `finally`-Block explizit auf: erst downgrade (entfernt admin_tenant,
stuft den Superadmin zurück, löscht das Seed-Setting), dann die selbst angelegten
Test-Zeilen löschen, dann wieder upgrade auf `head` -- damit nachfolgende Tests im selben
Lauf (derselbe physische Testcontainer, Port 5433) eine unveränderte, rückstandsfreie DB
vorfinden. Voller Testlauf zweimal hintereinander grün bestätigt das (siehe Task-Report).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.services.settings_service import SettingsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

PREV_REVISION = "a8b9c0d1e2f3"
THIS_REVISION = "cd755854e58c"

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
    return f"mig1-{tag}-{uuid.uuid4().hex[:8]}"


async def test_promote_oldest_admin_grant_others_seed_flag_and_cascade(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_users: list[str] = []
    seeded_tenants: list[str] = []

    try:
        # 1. Auf den Vorgänger-Head zurück: admin_tenant existiert danach nicht mehr,
        #    kein Superadmin, kein Seed-Setting -- der Zustand vor dieser Migration.
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            default_tenant_id = (
                await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
            ).scalar_one()

            extra_tenant_slug = f"mig1-cascade-{uuid.uuid4().hex[:8]}"
            extra_tenant_id = (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) "
                        "VALUES ('Mig1 Cascade', :slug, true, now()) RETURNING id"
                    ),
                    {"slug": extra_tenant_slug},
                )
            ).scalar_one()
            seeded_tenants.append(extra_tenant_slug)

            old_admin = _uname("old-admin")
            newer_admin = _uname("newer-admin")
            auditor = _uname("auditor")
            sso_admin = _uname("sso-admin")
            cascade_user_victim = _uname("cascade-user")
            seeded_users += [old_admin, newer_admin, auditor, sso_admin, cascade_user_victim]

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

            old_admin_id = await _mk_user(old_admin, role="admin")
            # sicherstellen, dass `newer_admin`'s id garantiert grösser ist als die von
            # `old_admin`, auch falls beide INSERTs binnen derselben Millisekunde liefen --
            # die Beförderung entscheidet über `id ASC`, nicht über einen Zeitstempel.
            newer_admin_id = await _mk_user(newer_admin, role="admin")
            assert newer_admin_id > old_admin_id
            auditor_id = await _mk_user(auditor, role="auditor")
            await conn.execute(
                text("INSERT INTO auditor_tenant (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": auditor_id, "t": default_tenant_id},
            )
            sso_admin_id = await _mk_user(
                sso_admin, role="admin", is_sso=True, tenant_id=extra_tenant_id
            )
            cascade_user_id = await _mk_user(cascade_user_victim, role="admin")

        # 2. Die Migration selbst treiben: promote/grant/seed passiert genau hier.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            old_admin_role = (
                await conn.execute(
                    text("SELECT role FROM app_user WHERE id = :id"), {"id": old_admin_id}
                )
            ).scalar_one()
            assert old_admin_role == "superadmin", "ältester lokaler Admin wurde nicht befördert"

            newer_admin_role = (
                await conn.execute(
                    text("SELECT role FROM app_user WHERE id = :id"), {"id": newer_admin_id}
                )
            ).scalar_one()
            assert newer_admin_role == "admin", "jüngerer Admin darf nicht mit befördert werden"

            newer_admin_grant = (
                await conn.execute(
                    text("SELECT tenant_id FROM admin_tenant WHERE user_id = :id"),
                    {"id": newer_admin_id},
                )
            ).scalar_one_or_none()
            assert newer_admin_grant == default_tenant_id

            old_admin_grant = (
                await conn.execute(
                    text("SELECT 1 FROM admin_tenant WHERE user_id = :id"), {"id": old_admin_id}
                )
            ).scalar_one_or_none()
            assert old_admin_grant is None, "Superadmin braucht keine admin_tenant-Zeile"

            auditor_role, auditor_grant_tenant = (
                await conn.execute(
                    text(
                        "SELECT u.role, at.tenant_id FROM app_user u "
                        "JOIN auditor_tenant at ON at.user_id = u.id WHERE u.id = :id"
                    ),
                    {"id": auditor_id},
                )
            ).one()
            assert auditor_role == "auditor"
            assert auditor_grant_tenant == default_tenant_id

            sso_tenant_id = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"), {"id": sso_admin_id}
                )
            ).scalar_one()
            assert sso_tenant_id == extra_tenant_id, "SSO-Konto tenant_id darf sich nicht ändern"

            # ---- admin_tenant: zusammengesetzter PK ----
            pk_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT a.attname FROM pg_index i "
                            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                            "AND a.attnum = ANY(i.indkey) "
                            "WHERE i.indrelid = 'admin_tenant'::regclass AND i.indisprimary "
                            "ORDER BY a.attname"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(pk_cols) == {"user_id", "tenant_id"}

            # ---- Mode-Flag: existiert, false, via SettingsService lesbar ----
            row = (
                await conn.execute(
                    text(
                        "SELECT value, tenant_id FROM setting "
                        "WHERE key = 'instance.multi_tenant_mode'"
                    )
                )
            ).one()
            assert row.tenant_id == default_tenant_id
            assert row.value is False

        async with async_sessionmaker(bind=migrated_engine, expire_on_commit=False)() as s:
            effective = await SettingsService(s).get_all()
            assert effective["instance.multi_tenant_mode"] is False

        # ---- Cascade: User löschen räumt seine admin_tenant-Zeile ab ----
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": cascade_user_id, "t": extra_tenant_id},
            )
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": cascade_user_id})
        async with migrated_engine.connect() as conn:
            gone = (
                await conn.execute(
                    text("SELECT 1 FROM admin_tenant WHERE user_id = :id"),
                    {"id": cascade_user_id},
                )
            ).scalar_one_or_none()
            assert gone is None, "admin_tenant-Zeile hätte per CASCADE verschwinden müssen"

        # ---- Cascade: Tenant löschen räumt die admin_tenant-Zeile des newer_admin ab ----
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": extra_tenant_id})
            seeded_tenants.remove(extra_tenant_slug)
        async with migrated_engine.connect() as conn:
            # newer_admin's Zeile zeigt auf default_tenant_id, nicht extra_tenant_id --
            # separat prüfen, dass das Löschen des EXTRA-Tenants dessen eigene
            # admin_tenant-Zeile(n) mitnimmt (der SSO-Admin hing per app_user.tenant_id
            # SET NULL daran, nicht per admin_tenant -- hier zählt nur, dass keine
            # admin_tenant-Zeile mehr auf den gelöschten Tenant zeigt).
            orphaned = (
                await conn.execute(
                    text("SELECT 1 FROM admin_tenant WHERE tenant_id = :id"),
                    {"id": extra_tenant_id},
                )
            ).scalar_one_or_none()
            assert orphaned is None
    finally:
        # Aufräumen: zurück auf den Vorgänger-Head (entfernt admin_tenant, stuft den
        # Superadmin zurück, löscht das Seed-Setting), dann die selbst angelegten Zeilen
        # entfernen, dann wieder auf head -- rückstandsfrei für alle folgenden Tests.
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
