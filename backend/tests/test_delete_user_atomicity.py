"""Atomicity of the account deletion and its ``USER_DELETED`` audit entry (M-03, L-04).

A deprovision/delete must land the row removal AND its audit entry in ONE transaction.
Before the fix, ``delete_user`` committed the audit BEFORE the deletion (L-04) and
``user_repo.delete`` committed the removal internally (M-03), so a failure in between left
either a phantom ``USER_DELETED`` for a still-existing account, or a silent deletion with no
audit trail. These tests drive a late failure and assert neither half survives.

The ``session`` fixture runs in ``create_savepoint`` join mode: a mid-function
``session.commit()`` RELEASES its savepoint into the outer transaction, so a later
``session.rollback()`` can no longer undo it -- exactly the property that made the old,
non-atomic ordering observable. With the fix both halves are only STAGED and roll back
together.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from app.api.routes.admin_users import delete_user
from app.models.audit import AuditLog
from app.models.user import AppUser
from app.repositories import user_repo
from app.services.audit import USER_DELETED
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _mk_user(session: AsyncSession, *, role: str) -> AppUser:
    u = AppUser(
        username=f"atom-{role}-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=role,
        is_sso=False,
        tenant_id=None,
    )
    session.add(u)
    await session.flush()
    return u


@pytest_asyncio.fixture
async def actor_and_target(session: AsyncSession) -> tuple[AppUser, AppUser]:
    # Superadmin caller skips the per-tenant scope checks; an auditor target avoids the
    # per-tenant-admin lockout guard -- a clean path straight to the audit+delete block.
    actor = await _mk_user(session, role="superadmin")
    target = await _mk_user(session, role="auditor")
    # Commit the seed so it is released into the outer transaction: a later
    # `session.rollback()` (simulating a failed request) then undoes ONLY the work staged by
    # `delete_user`, not the seed itself -- otherwise "the account survives" could not be
    # observed at all (nothing was committed to survive).
    await session.commit()
    return actor, target


async def test_delete_user_atomic_when_deletion_fails_after_audit(
    session: AsyncSession,
    actor_and_target: tuple[AppUser, AppUser],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure AFTER the audit is staged but BEFORE the final commit must roll BOTH back:
    the account still exists and NO ``USER_DELETED`` entry is persisted."""
    actor, target = actor_and_target
    target_id = target.id
    target_username = target.username
    assert target_id is not None

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated deletion failure")

    # Fail on the actual row removal -- the last step before the final commit.
    monkeypatch.setattr(user_repo, "delete", _boom)

    with pytest.raises(RuntimeError):
        await delete_user(None, actor, target_id, session)  # type: ignore[arg-type]

    # Simulate the request teardown on an unhandled error: nothing gets committed.
    await session.rollback()

    assert await user_repo.get(session, target_id) is not None, "account must survive"
    res = await session.execute(
        select(AuditLog).where(AuditLog.action == USER_DELETED, AuditLog.target == target_username)
    )
    assert res.first() is None, "no phantom USER_DELETED audit entry may persist"


async def test_delete_user_happy_path_deletes_and_audits_exactly_once(
    session: AsyncSession,
    actor_and_target: tuple[AppUser, AppUser],
) -> None:
    """Regression: a normal deletion removes the account AND leaves exactly one
    ``USER_DELETED`` entry (both committed together)."""
    actor, target = actor_and_target
    target_id = target.id
    target_username = target.username
    assert target_id is not None

    await delete_user(None, actor, target_id, session)  # type: ignore[arg-type]

    assert await user_repo.get(session, target_id) is None, "account must be gone"
    res = await session.execute(
        select(AuditLog).where(AuditLog.action == USER_DELETED, AuditLog.target == target_username)
    )
    assert len(res.scalars().all()) == 1, "exactly one USER_DELETED entry expected"
