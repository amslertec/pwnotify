"""Verifiziert Migration `798b22bf8252` (`assignment_group.role`) direkt.

Wie `test_migration_group_member_snapshot.py`: die savepoint-isolierte `session`-Fixture
reicht nicht -- `migrated_engine` hat diese Migration bereits vor jedem Test gegen eine
leere DB gefahren, es gibt also nichts mehr zu upgraden. Dieser Test treibt die Migration
daher selbst: downgrade auf den Vorgänger-Head, eine `assignment_group`-Zeile OHNE `role`
committen (die Spalte existiert dort noch nicht), upgrade zurück auf `THIS_REVISION`,
Backfill prüfen.

Läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) -- der
`finally`-Block räumt deshalb explizit auf: downgrade (entfernt die Spalte wieder), selbst
angelegte Test-Zeilen löschen, wieder upgrade auf `head` -- damit nachfolgende Tests im
selben Lauf eine unveränderte, rückstandsfreie DB vorfinden. Der downgrade/upgrade-Zyklus
im `finally` ist zugleich der geforderte Round-Trip-Test.
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

PREV_REVISION = "8573de47a2a1"
THIS_REVISION = "798b22bf8252"

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


def _slug(tag: str) -> str:
    return f"mig-role-{tag}-{uuid.uuid4().hex[:8]}"


async def test_role_column_exists_and_backfills_admin(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_group_ids: list[int] = []

    try:
        # 1. Auf den Vorgänger-Head zurück: `role` existiert noch nicht -- der Zustand vor
        #    dieser Migration.
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            group_slug = _slug("existing")
            group_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :g, now()) RETURNING id"
                    ),
                    {"n": "Mig-Role Group", "g": group_slug},
                )
            ).scalar_one()
            seeded_group_ids.append(group_id)

        # 2. Die Migration selbst treiben: add_column mit server_default passiert genau
        #    hier -- backfillt die bereits vorhandene Zeile auf "admin".
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            # ---- assignment_group.role: existiert, NOT NULL ----
            col = (
                await conn.execute(
                    text(
                        "SELECT is_nullable, character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'assignment_group' AND column_name = 'role'"
                    )
                )
            ).one()
            assert col.is_nullable == "NO"
            assert col.character_maximum_length == 16

            # ---- Backfill: bestehende Zeile bekommt "admin" ----
            role = (
                await conn.execute(
                    text("SELECT role FROM assignment_group WHERE id = :id"),
                    {"id": group_id},
                )
            ).scalar_one()
            assert role == "admin", "bestehende Gruppe muss per server_default 'admin' bekommen"

        # ---- Insert ohne role (neue Zeile): greift ebenfalls der server_default ----
        new_slug = _slug("new")
        async with migrated_engine.begin() as conn:
            new_group_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :g, now()) RETURNING id"
                    ),
                    {"n": "Mig-Role New Group", "g": new_slug},
                )
            ).scalar_one()
            seeded_group_ids.append(new_group_id)

        async with migrated_engine.connect() as conn:
            new_role = (
                await conn.execute(
                    text("SELECT role FROM assignment_group WHERE id = :id"),
                    {"id": new_group_id},
                )
            ).scalar_one()
            assert new_role == "admin"

            # ---- NOT NULL wird durchgesetzt: expliziter NULL-Insert scheitert ----
        async with migrated_engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at, role) "
                        "VALUES (:n, :g, now(), NULL)"
                    ),
                    {"n": "Mig-Role Null Group", "g": _slug("null")},
                )
            await trans.rollback()
    finally:
        # Downgrade: entfernt die role-Spalte -- der Zustand vor dieser Migration. Zugleich
        # der geforderte Round-Trip-Test.
        await _downgrade(PREV_REVISION)
        async with migrated_engine.begin() as conn:
            if seeded_group_ids:
                await conn.execute(
                    text("DELETE FROM assignment_group WHERE id = ANY(:ids)"),
                    {"ids": seeded_group_ids},
                )
        await _upgrade("head")
