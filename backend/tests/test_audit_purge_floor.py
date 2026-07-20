"""M3: audit-trail purge must have a non-erasable floor, must be audited, and must not
commit the caller's transaction; L4: privacy.*_retention_days must reject bad values.

Threat model (M3): an admin who wants to erase their tracks lowers ``audit.retention_days``
in small steps (365 -> 180 -> ... -> 1), triggering a purge each time. Every single step
stays under the >50% brake (and slices below ``_MIN_COUNT`` bypass it entirely), so after a
handful of iterations the whole trail -- including the SETTINGS_CHANGED entries that document
the shrinking -- is gone. A hard floor on the retention window closes this: the most recent
FLOOR days are always kept, so the tamper evidence survives every iteration.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.errors import ValidationError
from app.models.audit import AuditLog
from app.repositories import audit_repo
from app.services import audit, runner
from app.services.retention import AUDIT_RETENTION_FLOOR_DAYS
from app.services.settings_schema import SETTINGS
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed(session: AsyncSession, *, count: int, age_days: float, action: str) -> None:
    at = dt.datetime.now(dt.UTC) - dt.timedelta(days=age_days)
    for _ in range(count):
        row = AuditLog(tenant_id=None, actor_type="system", action=action, detail={})
        row.at = at
        session.add(row)
    await session.flush()


# --- validator: 0 OR >= FLOOR, reject the 1..FLOOR-1 band ------------------------ #
def test_validator_rejects_below_floor() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    with pytest.raises(ValidationError) as ei:
        validate(10)  # inside the 1..FLOOR-1 band
    assert ei.value.status_code == 400


def test_validator_rejects_floor_minus_one() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    with pytest.raises(ValidationError):
        validate(AUDIT_RETENTION_FLOOR_DAYS - 1)


def test_validator_allows_zero_floor_and_above() -> None:
    validate = SETTINGS["audit.retention_days"].validate
    assert validate is not None
    assert validate(0) == 0  # keep forever stays allowed
    assert validate(AUDIT_RETENTION_FLOOR_DAYS) == AUDIT_RETENTION_FLOOR_DAYS
    assert validate(90) == 90


# --- floor protects the recent trail against the iterative shrink attack ---------- #
async def test_floor_protects_recent_entries_against_iterative_purge(
    session: AsyncSession,
) -> None:
    await session.execute(sa_delete(AuditLog))
    # Small slices (< _MIN_COUNT) so the >50% brake never engages -- exactly the bypass the
    # attack relies on. Two young slices sit inside the floor and MUST survive.
    await _seed(session, count=5, age_days=100, action="test.old_a")
    await _seed(session, count=5, age_days=50, action="test.old_b")
    await _seed(session, count=5, age_days=40, action="test.old_c")
    await _seed(session, count=5, age_days=10, action="test.young_a")  # < FLOOR
    await _seed(session, count=5, age_days=2, action="test.young_b")  # < FLOOR

    # The attacker lowers the window step by step and purges after each step.
    for days in (90, 45, 35, 5, 1):
        await audit_repo.purge_older_than(session, days=days)

    floor_cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=AUDIT_RETENTION_FLOOR_DAYS)
    survivors = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.at >= floor_cutoff)
        )
    ).scalar_one()
    # Both young slices (10 rows) are younger than the floor and must all remain.
    assert survivors == 10


async def test_purge_clamps_window_to_floor(session: AsyncSession) -> None:
    await session.execute(sa_delete(AuditLog))
    # A single row aged between 1 and FLOOR days: a sub-floor window must NOT delete it.
    await _seed(session, count=1, age_days=AUDIT_RETENTION_FLOOR_DAYS - 5, action="test.recent")

    removed = await audit_repo.purge_older_than(session, days=1)

    assert removed == 0
    remaining = (await session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert remaining == 1


# --- the purge itself is audited -------------------------------------------------- #
async def test_purge_is_audited(session: AsyncSession) -> None:
    await session.execute(sa_delete(AuditLog))
    await _seed(session, count=25, age_days=100, action="test.old")
    await _seed(session, count=100, age_days=1, action="test.recent")

    steps = await runner._apply_audit_retention(
        session, {"audit.retention_days": AUDIT_RETENTION_FLOOR_DAYS}
    )

    assert {"step": "audit_purge", "removed": 25} in steps
    entries = (
        (await session.execute(select(AuditLog).where(AuditLog.action == audit.AUDIT_PURGED)))
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].detail["removed"] == 25
    assert entries[0].actor_type == "system"
    # Owner-context test session -> no active tenant -> NULL attribution (see report).
    assert entries[0].tenant_id is None


async def test_no_purge_no_audit_entry(session: AsyncSession) -> None:
    await session.execute(sa_delete(AuditLog))
    await _seed(session, count=3, age_days=1, action="test.recent")  # nothing old enough

    steps = await runner._apply_audit_retention(session, {"audit.retention_days": 90})

    assert steps == []
    entries = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == audit.AUDIT_PURGED)
        )
    ).scalar_one()
    assert entries == 0


# --- foreign-commit removed ------------------------------------------------------- #
async def test_purge_does_not_commit_caller_transaction(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session.execute(sa_delete(AuditLog))
    await _seed(session, count=25, age_days=100, action="test.old")
    await _seed(session, count=100, age_days=1, action="test.recent")

    commits: list[int] = []
    orig_commit = session.commit

    async def _spy() -> None:
        commits.append(1)
        await orig_commit()

    monkeypatch.setattr(session, "commit", _spy)
    removed = await audit_repo.purge_older_than(session, days=AUDIT_RETENTION_FLOOR_DAYS)

    assert removed == 25
    assert commits == [], "purge must not commit the caller's in-flight transaction"


# --- L4: privacy retention validators -------------------------------------------- #
@pytest.mark.parametrize("key", ["privacy.user_retention_days", "privacy.log_retention_days"])
def test_privacy_retention_rejects_negative(key: str) -> None:
    validate = SETTINGS[key].validate
    assert validate is not None, f"{key} must carry a validator"
    with pytest.raises(ValidationError) as ei:
        validate(-5)
    assert ei.value.status_code == 400


@pytest.mark.parametrize("key", ["privacy.user_retention_days", "privacy.log_retention_days"])
def test_privacy_retention_rejects_non_numeric(key: str) -> None:
    validate = SETTINGS[key].validate
    assert validate is not None
    with pytest.raises(ValidationError):
        validate("forever")


@pytest.mark.parametrize("key", ["privacy.user_retention_days", "privacy.log_retention_days"])
def test_privacy_retention_allows_zero_and_positive(key: str) -> None:
    validate = SETTINGS[key].validate
    assert validate is not None
    assert validate(0) == 0
    assert validate(365) == 365
