"""Verifiziert Migration `8573de47a2a1` (`assignment_group.last_synced_at` +
`assignment_group_member`-Snapshot-Tabelle) direkt.

Wie `test_migration_console_groups.py`/`test_migration_is_default_home.py`: die
savepoint-isolierte `session`-Fixture reicht nicht -- `migrated_engine` hat diese
Migration bereits vor jedem Test gegen eine leere DB gefahren, es gibt also nichts mehr
zu upgraden. Dieser Test treibt die Migration daher selbst: downgrade auf den
Vorgänger-Head, eine `assignment_group`-Zeile committen (die Tabelle existiert dort
bereits, seit `5d152bfe7585`), upgrade zurück auf `THIS_REVISION`, Ergebnis prüfen.

Läuft auf einer eigenen, ECHT committeten Verbindung (kein Savepoint-Rollback) -- der
`finally`-Block räumt deshalb explizit auf: downgrade (entfernt Tabelle/Spalte wieder),
selbst angelegte Test-Zeilen löschen, wieder upgrade auf `head` -- damit nachfolgende
Tests im selben Lauf (derselbe physische Testcontainer, Port 5433) eine unveränderte,
rückstandsfreie DB vorfinden. Der downgrade/upgrade-Zyklus im `finally` ist zugleich der
geforderte Round-Trip-Test.
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

PREV_REVISION = "5d152bfe7585"
THIS_REVISION = "8573de47a2a1"

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
    return f"mig4-{tag}-{uuid.uuid4().hex[:8]}"


async def test_last_synced_at_and_member_snapshot_table(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) garantiert: PWNOTIFY_DATABASE_URL zeigt bereits
    auf die Test-DB und die Settings-Cache ist entsprechend umgebogen -- die direkten
    `command.downgrade`/`command.upgrade`-Aufrufe unten treffen also dieselbe DB."""
    seeded_group_ids: list[int] = []

    try:
        # 1. Auf den Vorgänger-Head zurück: `last_synced_at` und `assignment_group_member`
        #    existieren noch nicht -- der Zustand vor dieser Migration.
        await _downgrade(PREV_REVISION)

        async with migrated_engine.begin() as conn:
            group_slug = _slug("group")
            group_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :g, now()) RETURNING id"
                    ),
                    {"n": "Mig4 Group", "g": group_slug},
                )
            ).scalar_one()
            seeded_group_ids.append(group_id)

            cascade_group_slug = _slug("cascade-group")
            cascade_group_id = (
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group (name, entra_group_id, created_at) "
                        "VALUES (:n, :g, now()) RETURNING id"
                    ),
                    {"n": "Mig4 Cascade Group", "g": cascade_group_slug},
                )
            ).scalar_one()
            seeded_group_ids.append(cascade_group_id)

        # 2. Die Migration selbst treiben: add_column + create_table passiert genau hier.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            # ---- assignment_group.last_synced_at: existiert, nullable, bestehende
            #      Zeilen bleiben NULL ----
            col = (
                await conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'assignment_group' "
                        "AND column_name = 'last_synced_at'"
                    )
                )
            ).one()
            assert col.is_nullable == "YES"

            last_synced = (
                await conn.execute(
                    text("SELECT last_synced_at FROM assignment_group WHERE id = :id"),
                    {"id": group_id},
                )
            ).scalar_one()
            assert last_synced is None, "bestehende Gruppe darf keinen Sync-Zeitstempel bekommen"

            # ---- assignment_group_member: erwartete Spalten ----
            member_cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'assignment_group_member'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(member_cols) == {
                "id",
                "assignment_group_id",
                "entra_id",
                "upn",
                "display_name",
                "mail",
                "synced_at",
            }

            # ---- FK assignment_group_member.assignment_group_id -> assignment_group.id ----
            fk_target = (
                await conn.execute(
                    text(
                        "SELECT confrelid::regclass::text FROM pg_constraint "
                        "WHERE conrelid = 'assignment_group_member'::regclass "
                        "AND contype = 'f'"
                    )
                )
            ).scalar_one()
            assert fk_target == "assignment_group"

        # ---- Insert einer Snapshot-Zeile über eine COMMITTENDE Verbindung -- sonst
        #      würde sie beim Schliessen des `async with`-Blocks implizit zurückgerollt
        #      (kein commit() aufgerufen) und der Duplicate-Check unten sähe keinen
        #      Konflikt. ----
        member_entra_id = str(uuid.uuid4())
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO assignment_group_member "
                    "(assignment_group_id, entra_id, upn, display_name, mail, synced_at) "
                    "VALUES (:gid, :eid, :upn, 'Mig4 User', 'mig4@example.test', now())"
                ),
                {
                    "gid": group_id,
                    "eid": member_entra_id,
                    "upn": "mig4-user@example.test",
                },
            )

        # ---- duplicate (assignment_group_id, entra_id) RAISES (composite unique) ----
        async with migrated_engine.connect() as conn:
            trans = await conn.begin()
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO assignment_group_member "
                        "(assignment_group_id, entra_id, upn, synced_at) "
                        "VALUES (:gid, :eid, 'other-upn@example.test', now())"
                    ),
                    {"gid": group_id, "eid": member_entra_id},
                )
            await trans.rollback()

        # ---- Cascade: assignment_group löschen räumt seine
        #      assignment_group_member-Zeilen ab ----
        cascade_entra_id = str(uuid.uuid4())
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO assignment_group_member "
                    "(assignment_group_id, entra_id, upn, synced_at) "
                    "VALUES (:gid, :eid, 'cascade-user@example.test', now())"
                ),
                {"gid": cascade_group_id, "eid": cascade_entra_id},
            )
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM assignment_group WHERE id = :id"), {"id": cascade_group_id}
            )
        async with migrated_engine.connect() as conn:
            gone_member = (
                await conn.execute(
                    text("SELECT 1 FROM assignment_group_member WHERE assignment_group_id = :id"),
                    {"id": cascade_group_id},
                )
            ).scalar_one_or_none()
            assert gone_member is None, (
                "assignment_group_member-Zeile hätte per CASCADE verschwinden müssen"
            )
            seeded_group_ids.remove(cascade_group_id)
    finally:
        # Downgrade: entfernt assignment_group_member (samt aller Zeilen) und
        # last_synced_at -- der Zustand vor dieser Migration. Zugleich der geforderte
        # Round-Trip-Test.
        await _downgrade(PREV_REVISION)
        async with migrated_engine.begin() as conn:
            if seeded_group_ids:
                await conn.execute(
                    text("DELETE FROM assignment_group WHERE id = ANY(:ids)"),
                    {"ids": seeded_group_ids},
                )
        await _upgrade("head")
