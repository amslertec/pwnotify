"""Angriffs-/Isolations-Testsuite: End-to-End-Beweis über den echten tenant_scoped_session-Pfad.

Diese Tests seeden zwei Tenants + je eine `run`-Zeile als Superuser (RLS-frei) und committen sie
ECHT -- `tenant_scoped_session` öffnet eine EIGENE Session/Verbindung und sieht daher keine
uncommitteten Daten. Die savepoint-isolierte `session`-Fixture (siehe conftest.py) eignet sich
dafür NICHT: ihr `commit()` löst nur die SAVEPOINT auf, die äußere, von der Fixture offen
gehaltene Transaktion bleibt bis zum Test-Teardown unbestätigt -- für eine zweite, echte
Verbindung ist das weiterhin unsichtbar (das haben wir empirisch bestätigt: der Read-Test sah
0 statt der erwarteten Zeile). Der Seed läuft daher über eine eigene Connection direkt auf
`migrated_engine` (autobegin + echtes `commit()`). Weil dieser Commit echt ist, greift die
Savepoint-Rücksetzung nicht -- die `seeded_tenants`-Fixture räumt darum im `finally` explizit
wieder auf (Tenant-Delete kaskadiert per ON DELETE CASCADE auf `run`/`setting`), sonst bleiben
Reststände in der Test-DB.

Drei Angriffs-/Isolationszusicherungen (CI-Pflicht, Design §4 Schicht 4):
1. Read-Isolation: tenant_scoped_session(A) sieht nur A's Zeilen.
2. Cross-Tenant-Write wird von RLS abgelehnt.
3. Fail-safe: App-Rolle ohne gültigen Tenant (leere GUC) liefert 0 Zeilen, keinen Crash, kein Leck.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.db.tenant_context import tenant_scoped_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


async def _seed_two_tenants(conn: AsyncConnection) -> tuple[int, int]:
    await conn.execute(
        text(
            "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
            "('AtkA','atk-a',true,now()), ('AtkB','atk-b',true,now())"
        )
    )
    a, b = (
        (
            await conn.execute(
                text("SELECT id FROM tenant WHERE slug IN ('atk-a','atk-b') ORDER BY slug")
            )
        )
        .scalars()
        .all()
    )
    await conn.execute(
        text(
            "INSERT INTO run "
            "(tenant_id, trigger, dry_run, status, started_at, "
            "checked_users, sent, failed, skipped, detail_log) VALUES "
            "(:a,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb), "
            "(:b,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb)"
        ),
        {"a": a, "b": b},
    )
    await conn.commit()
    return a, b


@pytest_asyncio.fixture
async def seeded_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[tuple[int, int]]:
    """Seedet zwei Tenants + je eine `run`-Zeile (echt committet, Superuser-Connection) und
    räumt sie danach wieder auf -- als Superuser, damit RLS den Cleanup nicht behindert."""
    async with migrated_engine.connect() as conn:
        a, b = await _seed_two_tenants(conn)
        try:
            yield a, b
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


async def test_tenant_scoped_session_sees_only_own_rows(
    seeded_tenants: tuple[int, int],
) -> None:
    a, _b = seeded_tenants
    async with tenant_scoped_session(a) as s:
        rows = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
    assert set(rows) == {a}, f"Leak: {a} sah {rows}"


async def test_cross_tenant_write_is_rejected(seeded_tenants: tuple[int, int]) -> None:
    a, b = seeded_tenants
    async with tenant_scoped_session(a) as s:
        with pytest.raises(Exception) as exc:
            await s.execute(
                text(
                    "INSERT INTO run "
                    "(tenant_id, trigger, dry_run, status, started_at, "
                    "checked_users, sent, failed, skipped, detail_log) "
                    "VALUES (:b,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb)"
                ),
                {"b": b},
            )
            await s.commit()
        assert "row-level security" in str(exc.value).lower()


async def test_missing_tenant_is_failsafe_empty(seeded_tenants: tuple[int, int]) -> None:
    # Kein aktiver Tenant, aber App-Rolle erzwungen: 0 Zeilen (kein Crash, kein Leak).
    # (Simuliert einen Programmfehler, bei dem der Kontext vergessen wurde.)
    async with tenant_scoped_session(1) as s:
        await s.execute(text("SELECT set_config('app.current_tenant','',true)"))
        rows = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
    assert rows == [], f"Fail-safe verletzt: sah {rows}"
