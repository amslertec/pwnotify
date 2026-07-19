"""H6: the audit trail must not be wipeable via audit.retention_days.

Two protections:
(a) the audit.retention_days validator rejects non-integer / negative values (400);
    0 (keep forever) and sane positive windows stay valid;
(b) purge_older_than applies the same >50% brake as privacy retention: a purge that would
    delete more than half of all audit rows is blocked (nothing deleted).
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.errors import ValidationError
from app.models.audit import AuditLog
from app.repositories import audit_repo
from app.services.settings_schema import SETTINGS
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


# --- (a) validator -------------------------------------------------------------- #
def test_retention_validator_rejects_negative() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    with pytest.raises(ValidationError) as ei:
        validate(-5)
    assert ei.value.status_code == 400


def test_retention_validator_rejects_fraction() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    with pytest.raises(ValidationError):
        validate(1.5)


def test_retention_validator_allows_zero_and_positive() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    assert validate(0) == 0
    assert validate(365) == 365


# --- (b) purge brake ------------------------------------------------------------ #
async def _seed_audit_rows(
    session: AsyncSession, *, count: int, at: dt.datetime, action: str
) -> None:
    for _ in range(count):
        row = AuditLog(
            tenant_id=None,
            actor_type="system",
            action=action,
            outcome="success",
            detail={},
        )
        row.at = at
        session.add(row)
    await session.flush()


async def test_purge_blocks_when_it_would_delete_more_than_half(session: AsyncSession) -> None:
    # Deterministic baseline: clear the table within the rolled-back savepoint.
    await session.execute(sa_delete(AuditLog))
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    await _seed_audit_rows(session, count=25, at=old, action="test.retention_guard")

    removed = await audit_repo.purge_older_than(session, days=1)

    assert removed == 0, "purge should be blocked, not delete the whole trail"
    remaining = (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert remaining == 25


async def test_purge_proceeds_for_a_small_fraction(session: AsyncSession) -> None:
    await session.execute(sa_delete(AuditLog))
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    now = dt.datetime.now(dt.UTC)
    # 25 old of 125 total = 20% -> below the 50% brake, purge proceeds.
    await _seed_audit_rows(session, count=25, at=old, action="test.retention_old")
    await _seed_audit_rows(session, count=100, at=now, action="test.retention_recent")

    removed = await audit_repo.purge_older_than(session, days=1)
    assert removed == 25
