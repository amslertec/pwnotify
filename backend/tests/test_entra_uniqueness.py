"""`entra_id` ist mandantensicher: unique nur pro `(tenant_id, entra_id)`, nicht mehr global.

Seed-Pattern wie `test_isolation_attack.py`: zwei Tenants + Zeilen als Superuser (RLS-frei),
ECHT committet über eine eigene Connection auf `migrated_engine` -- die savepoint-isolierte
`session`-Fixture eignet sich hier nicht (siehe Kommentar dort), weil IntegrityError innerhalb
einer SAVEPOINT-Transaktion die äußere Transaktion für Folge-Statements abbrechen würde und wir
zwei unabhängige Einfüge-Versuche brauchen.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_ENTRA_COLS = (
    "tenant_id, entra_id, upn, display_name, other_mails, account_enabled, "
    "password_never_expires, excluded, is_shared, raw, last_synced_at"
)
_ENTRA_VALS = (
    ":tid, :entra_id, :upn, '', '[]'::jsonb, true, false, false, false, '{}'::jsonb, now()"
)


async def _insert_entra_user(
    conn: AsyncConnection, tenant_id: int, entra_id: str, upn: str
) -> None:
    await conn.execute(
        text(f"INSERT INTO entra_user ({_ENTRA_COLS}) VALUES ({_ENTRA_VALS})"),
        {"tid": tenant_id, "entra_id": entra_id, "upn": upn},
    )


@pytest_asyncio.fixture
async def seeded_tenants(migrated_engine: AsyncEngine) -> AsyncGenerator[tuple[int, int]]:
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                "('EntraA','entra-a',true,now()), ('EntraB','entra-b',true,now())"
            )
        )
        a, b = (
            (
                await conn.execute(
                    text("SELECT id FROM tenant WHERE slug IN ('entra-a','entra-b') ORDER BY slug")
                )
            )
            .scalars()
            .all()
        )
        await conn.commit()
        try:
            yield a, b
        finally:
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


async def test_same_entra_id_allowed_across_two_tenants(
    seeded_tenants: tuple[int, int], migrated_engine: AsyncEngine
) -> None:
    a, b = seeded_tenants
    async with migrated_engine.connect() as conn:
        await _insert_entra_user(conn, a, "same-entra-id-123", "user@a.example")
        await _insert_entra_user(conn, b, "same-entra-id-123", "user@b.example")
        await conn.commit()

        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT tenant_id FROM entra_user WHERE entra_id = 'same-entra-id-123' "
                        "ORDER BY tenant_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(rows) == {a, b}, f"Erwartet beide Tenants, sah {rows}"


async def test_same_entra_id_twice_in_one_tenant_raises(
    seeded_tenants: tuple[int, int], migrated_engine: AsyncEngine
) -> None:
    a, _b = seeded_tenants
    async with migrated_engine.connect() as conn:
        await _insert_entra_user(conn, a, "dup-entra-id-456", "user1@a.example")
        await conn.commit()

        with pytest.raises(IntegrityError):
            await _insert_entra_user(conn, a, "dup-entra-id-456", "user2@a.example")
            await conn.commit()
