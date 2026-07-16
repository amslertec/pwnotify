"""Tests für den Tenant-Kontext: ContextVar-Roundtrip + begin-Listener-Beweis.

Der begin-Listener (registriert auf der App-Engine in session.py) trägt bei aktivem
ContextVar SET LOCAL ROLE + SET LOCAL app.current_tenant pro Transaktion ein. Diese Tests
laufen bewusst NICHT über die savepoint-isolierte `session`-Fixture (die bindet an eine
bereits laufende äußere Transaktion -> SAVEPOINT statt BEGIN, der begin-Event feuert dort
nicht zuverlässig), sondern über die echte App-Engine (`get_engine`/`get_session_factory`),
die auf die migrierte Test-DB zeigt.
"""

from __future__ import annotations

from app.db import session as db_session
from app.db.tenant_context import current_tenant_id, tenant_scoped_session, use_tenant
from sqlalchemy import text


async def test_context_var_roundtrip():
    assert current_tenant_id.get() is None
    async with use_tenant(42):
        assert current_tenant_id.get() == 42
    assert current_tenant_id.get() is None


async def test_context_var_resets_on_exception():
    try:
        async with use_tenant(7):
            assert current_tenant_id.get() == 7
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_tenant_id.get() is None


async def test_no_tenant_context_stays_owner_role(migrated_engine):
    # Ohne aktiven Tenant (ContextVar == None, plain get_session): kein Rollenwechsel.
    assert current_tenant_id.get() is None
    async for s in db_session.get_session():
        role = (await s.execute(text("SELECT current_user"))).scalar_one()
        assert role == "pwnotify"


async def test_begin_listener_sets_role_and_guc(migrated_engine):
    """Der eigentliche Beweis: innerhalb tenant_scoped_session(...) läuft die Transaktion
    als pwnotify_app-Rolle mit gesetztem app.current_tenant-GUC."""
    async with tenant_scoped_session(42) as session:
        role = (await session.execute(text("SELECT current_user"))).scalar_one()
        assert role == "pwnotify_app"

        guc = (
            await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()
        assert guc == "42"


async def test_tenant_scoped_session_does_not_leak_across_blocks(migrated_engine):
    """Pool-Sicherheit: zwei aufeinanderfolgende tenant_scoped_session-Blöcke mit
    unterschiedlichen Tenants dürfen sich Rolle/GUC nicht teilen (auch bei Connection-Reuse)."""
    async with tenant_scoped_session(1) as session_a:
        role_a = (await session_a.execute(text("SELECT current_user"))).scalar_one()
        guc_a = (
            await session_a.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()

    assert current_tenant_id.get() is None  # ContextVar wieder zurückgesetzt zwischen Blöcken

    async with tenant_scoped_session(2) as session_b:
        role_b = (await session_b.execute(text("SELECT current_user"))).scalar_one()
        guc_b = (
            await session_b.execute(text("SELECT current_setting('app.current_tenant', true)"))
        ).scalar_one()

    assert role_a == role_b == "pwnotify_app"
    assert guc_a == "1"
    assert guc_b == "2"
    assert guc_a != guc_b


async def test_owner_session_after_tenant_scoped_block_has_no_residue(migrated_engine):
    # Nach einem tenant_scoped_session-Block muss eine plain get_session-Session wieder
    # Owner sein -- kein Leck der Rolle/des GUC auf andere Sessions/Connections.
    async with tenant_scoped_session(5) as session:
        role = (await session.execute(text("SELECT current_user"))).scalar_one()
        assert role == "pwnotify_app"

    async for s in db_session.get_session():
        role = (await s.execute(text("SELECT current_user"))).scalar_one()
        assert role == "pwnotify"
