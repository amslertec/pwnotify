"""Verifiziert Migration `5d152bfe7585` (app_user.email, `source` auf den Grant-Tabellen,
`user_token`, `assignment_group`/`assignment_group_tenant`) direkt.

Wie `test_migration_access_model.py`/`test_migration_is_default_home.py`: die
savepoint-isolierte `session`-Fixture reicht nicht -- der Server-Default-Backfill auf
`source` läuft nur EINMAL, beim `upgrade()` selbst, und `migrated_engine` hat die
Migration bereits vor jedem Test gegen eine leere DB gefahren. Dieser Test treibt die
Migration daher selbst: downgrade auf den Vorgänger-Head, Testdaten (eine VOR der Migration
angelegte `admin_tenant`/`auditor_tenant`-Zeile ohne `source`-Spalte) committen, upgrade
zurück auf `THIS_REVISION`, Ergebnis prüfen.

Läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) -- der
`finally`-Block räumt deshalb explizit auf: downgrade (entfernt die neuen Tabellen/Spalten
wieder), selbst angelegte Test-Zeilen löschen, wieder upgrade auf `head` -- damit
nachfolgende Tests im selben Lauf (derselbe physische Testcontainer, Port 5433) eine
unveränderte, rückstandsfreie DB vorfinden. Der downgrade/upgrade-Zyklus im `finally`
ist zugleich der geforderte Round-Trip-Test. Voller Testlauf zweimal hintereinander
grün bestätigt das (siehe Task-Report).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

PREV_REVISION = "4035552093e2"
THIS_REVISION = "5d152bfe7585"

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


async def test_email_source_backfill_new_tables_and_cascades(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_users: list[str] = []
    seeded_groups: list[str] = []

    try:
        # 1. Auf den Vorgänger-Head zurück: email/source/user_token/assignment_group(_tenant)
        #    existieren noch nicht -- der Zustand vor dieser Migration.
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            default_tenant_id = (
                await conn.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
            ).scalar_one()

            admin_user = _uname("admin")
            auditor_user = _uname("auditor")
            cascade_user = _uname("cascade")
            seeded_users += [admin_user, auditor_user, cascade_user]

            async def _mk_user(username: str) -> int:
                return (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, password_hash, role, is_active, "
                            "is_sso, failed_login_count, created_at, updated_at) "
                            "VALUES (:u, 'x', 'admin', true, false, 0, now(), now()) "
                            "RETURNING id"
                        ),
                        {"u": username},
                    )
                ).scalar_one()

            admin_user_id = await _mk_user(admin_user)
            auditor_user_id = await _mk_user(auditor_user)
            cascade_user_id = await _mk_user(cascade_user)

            # Pre-existing Grant-Zeilen VOR der Migration -- die `source`-Spalte gibt es
            # hier noch nicht, genau der Bestand, den der server_default backfillen muss.
            await conn.execute(
                text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": admin_user_id, "t": default_tenant_id},
            )
            await conn.execute(
                text("INSERT INTO auditor_tenant (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": auditor_user_id, "t": default_tenant_id},
            )
            await conn.execute(
                text("INSERT INTO admin_tenant (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": cascade_user_id, "t": default_tenant_id},
            )

        # 2. Die Migration selbst treiben: add_column + create_table passiert genau hier.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            # ---- app_user.email: existiert, nullable, bestehende Konten bleiben NULL ----
            col = (
                await conn.execute(
                    text(
                        "SELECT is_nullable, data_type, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'app_user' AND column_name = 'email'"
                    )
                )
            ).one()
            assert col.is_nullable == "YES"
            assert col.character_maximum_length == 320

            email_val = (
                await conn.execute(
                    text("SELECT email FROM app_user WHERE id = :id"), {"id": admin_user_id}
                )
            ).scalar_one()
            assert email_val is None, "bestehendes Konto darf keine E-Mail bekommen"

            # ---- source-Backfill auf beiden Grant-Tabellen ----
            admin_source = (
                await conn.execute(
                    text("SELECT source FROM admin_tenant WHERE user_id = :id"),
                    {"id": admin_user_id},
                )
            ).scalar_one()
            assert admin_source == "manual"

            auditor_source = (
                await conn.execute(
                    text("SELECT source FROM auditor_tenant WHERE user_id = :id"),
                    {"id": auditor_user_id},
                )
            ).scalar_one()
            assert auditor_source == "manual"

            # ---- assignment_group + assignment_group_tenant: erwartete Spalten/FKs ----
            group_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'assignment_group'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(group_cols) == {"id", "name", "entra_group_id", "created_at"}

            group_tenant_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'assignment_group_tenant'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(group_tenant_cols) == {"assignment_group_id", "tenant_id"}

            group_pk_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT a.attname FROM pg_index i "
                            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                            "AND a.attnum = ANY(i.indkey) "
                            "WHERE i.indrelid = 'assignment_group_tenant'::regclass "
                            "AND i.indisprimary ORDER BY a.attname"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(group_pk_cols) == {"assignment_group_id", "tenant_id"}

            # ---- user_token: existiert, erwartete Spalten, purpose 'invite'/'reset' ----
            token_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'user_token'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(token_cols) == {
                "id",
                "app_user_id",
                "purpose",
                "token_hash",
                "expires_at",
                "consumed_at",
                "created_by",
                "created_at",
            }

        # Inserts über eine COMMITTENDE Verbindung (`begin()`), nicht die read-only
        # `connect()`-Verbindung oben -- sonst würden die Zeilen beim Schliessen des
        # `async with`-Blocks implizit zurückgerollt (kein `commit()` aufgerufen) und der
        # anschliessende Duplicate-Check unten sähe gar keinen Konflikt.
        invite_token_hash = uuid.uuid4().hex
        reset_token_hash = uuid.uuid4().hex
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_token (app_user_id, purpose, token_hash, expires_at, "
                    "created_by, created_at) "
                    "VALUES (:uid, 'invite', :th, now() + interval '7 days', :uid, now())"
                ),
                {"uid": admin_user_id, "th": invite_token_hash},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_token (app_user_id, purpose, token_hash, expires_at, "
                    "created_by, created_at) "
                    "VALUES (:uid, 'reset', :th, now() + interval '1 hour', :uid, now())"
                ),
                {"uid": admin_user_id, "th": reset_token_hash},
            )

        async with migrated_engine.connect() as conn:
            purposes = (
                (
                    await conn.execute(
                        text(
                            "SELECT purpose FROM user_token WHERE app_user_id = :id "
                            "ORDER BY purpose"
                        ),
                        {"id": admin_user_id},
                    )
                )
                .scalars()
                .all()
            )
            assert purposes == ["invite", "reset"]

        # ---- duplicate token_hash RAISES (unique) ----
        async with migrated_engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO user_token (app_user_id, purpose, token_hash, expires_at, "
                        "created_by, created_at) "
                        "VALUES (:uid, 'invite', :th, now() + interval '7 days', :uid, now())"
                    ),
                    {"uid": admin_user_id, "th": invite_token_hash},
                )
            await trans.rollback()

        # ---- Cascade: app_user löschen räumt seine user_token- und admin_tenant-Zeilen ab ----
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": cascade_user_id})
        async with migrated_engine.connect() as conn:
            gone_grant = (
                await conn.execute(
                    text("SELECT 1 FROM admin_tenant WHERE user_id = :id"),
                    {"id": cascade_user_id},
                )
            ).scalar_one_or_none()
            assert gone_grant is None, "admin_tenant-Zeile hätte per CASCADE verschwinden müssen"

        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": admin_user_id})
        async with migrated_engine.connect() as conn:
            gone_tokens = (
                await conn.execute(
                    text("SELECT 1 FROM user_token WHERE app_user_id = :id"),
                    {"id": admin_user_id},
                )
            ).scalar_one_or_none()
            assert gone_tokens is None, "user_token-Zeilen hätten per CASCADE verschwinden müssen"

        # ---- Cascade: assignment_group löschen räumt seine assignment_group_tenant-Zeilen ab ----
        group_slug = _uname("group")
        seeded_groups.append(group_slug)
        async with migrated_engine.begin() as conn:
            group_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :g, now()) RETURNING id"
                    ),
                    {"n": "Mig3 Group", "g": group_slug},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO assignment_group_tenant (assignment_group_id, tenant_id) "
                    "VALUES (:g, :t)"
                ),
                {"g": group_id, "t": default_tenant_id},
            )
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM assignment_group WHERE id = :id"), {"id": group_id}
            )
        async with migrated_engine.connect() as conn:
            gone_membership = (
                await conn.execute(
                    text("SELECT 1 FROM assignment_group_tenant WHERE assignment_group_id = :id"),
                    {"id": group_id},
                )
            ).scalar_one_or_none()
            assert gone_membership is None, (
                "assignment_group_tenant-Zeile hätte per CASCADE verschwinden müssen"
            )
            seeded_groups.remove(group_slug)
    finally:
        # Downgrade: entfernt user_token, assignment_group(_tenant) samt aller Zeilen (die
        # Tabellen selbst verschwinden -- kein separates Aufräumen von `seeded_groups`
        # nötig, `drop_table` nimmt sie mit), source-Spalten, email -- der Zustand vor
        # dieser Migration. Zugleich der geforderte Round-Trip-Test.
        await _downgrade(PREV_REVISION)
        async with migrated_engine.begin() as conn:
            if seeded_users:
                await conn.execute(
                    text("DELETE FROM app_user WHERE username = ANY(:names)"),
                    {"names": seeded_users},
                )
        await _upgrade("head")
