"""Verifies the DB-level CHECK constraint on `assignment_group.role` (L7).

The application layer already narrows `role` to `Literal["admin", "auditor"]`, but the
column itself was `String(16)` with only a NOT NULL guard -- the database accepted any
string. Defense-in-depth: a raw INSERT/UPDATE with a bogus role must be rejected by the
DB. `migrated_engine` has all migrations (incl. the new CHECK-constraint revision) applied,
so we drive raw SQL against it directly and assert the constraint fires.

Rows are seeded on a real committed connection and removed in the `finally` block so the
session-scoped DB stays clean for later tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


def _slug(tag: str) -> str:
    return f"mig-role-check-{tag}-{uuid.uuid4().hex[:8]}"


async def _insert_role(engine: AsyncEngine, role: str) -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO assignment_group (name, entra_group_id, created_at, role) "
                    "VALUES (:n, :g, now(), :r) RETURNING id"
                ),
                {"n": "Mig-Role-Check Group", "g": _slug(role), "r": role},
            )
        ).scalar_one()


async def test_role_check_constraint_rejects_invalid(migrated_engine: AsyncEngine) -> None:
    seeded_ids: list[int] = []
    try:
        # ---- valid roles are accepted ----
        for good in ("admin", "auditor"):
            seeded_ids.append(await _insert_role(migrated_engine, good))

        # ---- an invalid role is rejected by the CHECK constraint ----
        with pytest.raises(IntegrityError):
            await _insert_role(migrated_engine, "hacker")

        # ---- an UPDATE to an invalid role is rejected too ----
        with pytest.raises(IntegrityError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE assignment_group SET role = :r WHERE id = :id"),
                    {"r": "root", "id": seeded_ids[0]},
                )
    finally:
        if seeded_ids:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM assignment_group WHERE id = ANY(:ids)"),
                    {"ids": seeded_ids},
                )
