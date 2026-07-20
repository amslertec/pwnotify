"""Verifies migration `38535d051a81` (homeless SSO -> default tenant) directly.

Like `test_migration_is_default_home.py`: the savepoint-isolated `session` fixture is not
enough -- the UPDATE runs only ONCE, during `upgrade()` itself, and `migrated_engine` has
already run the migration against an empty DB before each test. This test therefore drives
the migration itself: downgrade to the predecessor head, commit test data, upgrade back to
`head`, check the result.

Runs on its own, REALLY committed connection (no savepoint rollback) -- the `finally`
block therefore explicitly cleans up: deletes the test rows it created itself, upgrades
back to `head` -- so subsequent tests in the same run (same physical test container, port
5433) find an unchanged, residue-free DB.
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
    # env.py internally runs via asyncio.run() -- must not run inside an already active
    # loop (see app/db/migrate.py::run_migrations); pytest-asyncio however already has a
    # loop open, so offload to a thread (same pattern as conftest.py).
    await asyncio.to_thread(command.downgrade, _alembic_config(), revision)


async def _upgrade(revision: str = "head") -> None:
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _uname(tag: str) -> str:
    return f"mig3-{tag}-{uuid.uuid4().hex[:8]}"


async def test_homeless_sso_accounts_healed_to_default_tenant(
    migrated_engine: AsyncEngine,
) -> None:
    """`migrated_engine` (session-scoped) guarantees: PWNOTIFY_DATABASE_URL already points
    at the test DB and the settings cache is redirected accordingly -- the direct
    `command.downgrade`/`command.upgrade` calls below therefore hit the same DB."""
    seeded_users: list[str] = []
    default_tenant_id: int | None = None
    customer_tenant_id: int | None = None
    customer_slug = f"mig3-customer-{uuid.uuid4().hex[:8]}"

    try:
        # 1. Back to the predecessor head: this migration's UPDATE has not run yet --
        #    the state before this migration (reproducing the prod legacy data).
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

            # Exactly the prod finding: SSO account without a home -- must be healed.
            homeless_sso_id = await _mk_user(homeless_sso, is_sso=True, tenant_id=None)
            # SSO account WITH a home (customer tenant) -- must remain untouched.
            homed_sso_id = await _mk_user(homed_sso, is_sso=True, tenant_id=customer_tenant_id)
            # NON-SSO account without a home -- not covered by this migration, must
            # remain untouched (NULL stays NULL).
            homeless_local_id = await _mk_user(homeless_local, is_sso=False, tenant_id=None)

        # 2. Drive the migration itself: the heal UPDATE happens right here.
        await _upgrade(THIS_REVISION)

        async with migrated_engine.connect() as conn:
            homeless_sso_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"),
                    {"id": homeless_sso_id},
                )
            ).scalar_one()
            assert homeless_sso_tid == default_tenant_id, (
                "homeless SSO account was not healed to the default tenant"
            )

            homed_sso_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"), {"id": homed_sso_id}
                )
            ).scalar_one()
            assert homed_sso_tid == customer_tenant_id, "already-homed SSO account must not change"

            homeless_local_tid = (
                await conn.execute(
                    text("SELECT tenant_id FROM app_user WHERE id = :id"),
                    {"id": homeless_local_id},
                )
            ).scalar_one()
            assert homeless_local_tid is None, (
                "NON-SSO account without a home must not be touched by this migration"
            )
    finally:
        # Cleanup: delete test rows, then upgrade back to `head` -- so subsequent tests in
        # the same run find an unchanged, residue-free DB.
        # (No downgrade needed: this migration's `downgrade()` is a deliberate no-op.)
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
