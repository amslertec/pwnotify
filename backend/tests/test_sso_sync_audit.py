"""Audit + lockout backstop of the scheduled SSO sync (findings M-02, L-03).

Two properties of the `oidc.sync_sso_users` deprovision pass that the earlier code did
not have:

* **M-02 (deletion is never silent):** a scheduled/direct sync that removes an SSO account
  MUST write a `USER_DELETED` entry (actor_type `system`, target UPN, sync's tenant) per
  removed account, and log the run overall with an aggregated `SSO_SYNCED`. Previously
  only the manual route wrote a summary entry; the scheduled runner path deleted silently.
  Against the old code, the USER_DELETED part is red.

* **L-03 (last-admin backstop):** if the SOLE admin of a tenant drops out of the group
  while the removal ratio (<=50%) would allow the removal, it must NOT be removed --
  consistent with the A4 backstop in `set_role`/`delete_user`. Against the old code, which
  only knew `removal_blocked_reason`, the last admin gets deleted (red).

Runs against real Postgres (port 5433, see `conftest.py`); Microsoft Graph is mocked
(`patch.object(oidc, "GraphClient", ...)`). Seed/cleanup with real commits.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from app.db.session import get_session_factory
from app.models.audit import AuditLog
from app.repositories import user_repo
from app.services import oidc
from app.services.audit import SSO_SYNCED, USER_DELETED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.asyncio

SETTINGS: dict[str, Any] = {
    "oidc.enabled": True,
    "oidc.admin_group_id": "admin-gruppe",
    "oidc.auditor_group_id": "auditor-gruppe",
    "graph.tenant_id": "t",
    "graph.client_id": "c",
    "graph.client_secret": "s",
    "graph.cloud": "global",
}


async def _create_tenant(engine: AsyncEngine, slug: str) -> int:
    async with engine.connect() as conn:
        tid = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "(:n, :s, true, now()) RETURNING id"
                    ),
                    {"n": slug, "s": slug},
                )
            ).scalar_one()
        )
        await conn.commit()
    return tid


async def _cleanup(engine: AsyncEngine, tenant_id: int, upn_like: str) -> None:
    async with engine.connect() as conn:
        # audit_log.tenant_id + app_user.tenant_id are FKs to tenant -- delete before the tenant.
        await conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tenant_id})
        await conn.execute(text("DELETE FROM app_user WHERE username LIKE :u"), {"u": upn_like})
        await conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tenant_id})
        await conn.commit()


async def _audit_rows(tenant_id: int, action: str) -> list[AuditLog]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
            )
        ).scalars()
        return list(rows)


# --------------------------------------------------------------------------------------- #
# M-02: a deprovision deletion is audited (USER_DELETED per account + aggregate).
# --------------------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def tenant_with_stale_sso(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """A tenant with a remaining SSO admin and a stale SSO auditor.

    The auditor (`stale`) is missing from the simulated desired set -> it must be removed
    and the removal audited. The admin remains (is in the admin group)."""
    tid = await _create_tenant(migrated_engine, "ssoaudit-m02")
    async with get_session_factory()() as session:
        await user_repo.create(
            session,
            username="keep-admin@ssoaudit.test",
            password_hash="x",
            role="admin",
            display_name="Keep Admin",
            is_sso=True,
            tenant_id=tid,
        )
        await user_repo.create(
            session,
            username="stale-auditor@ssoaudit.test",
            password_hash="x",
            role="auditor",
            display_name="Stale Auditor",
            is_sso=True,
            tenant_id=tid,
        )
    try:
        yield tid
    finally:
        await _cleanup(migrated_engine, tid, "%@ssoaudit.test")


def _graph_keep_admin_only() -> MagicMock:
    async def _members(group_id: str) -> list[dict[str, Any]]:
        if group_id == "admin-gruppe":
            return [{"userPrincipalName": "keep-admin@ssoaudit.test", "displayName": "Keep Admin"}]
        return []

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_members)
    fake.aclose = AsyncMock()
    return fake


async def test_deprovision_removal_is_audited(tenant_with_stale_sso: int) -> None:
    tid = tenant_with_stale_sso
    with patch.object(oidc, "GraphClient", return_value=_graph_keep_admin_only()):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, SETTINGS, tenant_id=tid)

    assert stats["removed"] == 1
    assert stats["synced"] == 1

    # M-02: exactly one USER_DELETED, system-attributed, target UPN, sync's tenant.
    deleted = await _audit_rows(tid, USER_DELETED)
    assert len(deleted) == 1, "deprovision deletion must write exactly one USER_DELETED"
    entry = deleted[0]
    assert entry.actor_type == "system"
    assert entry.target == "stale-auditor@ssoaudit.test"
    assert entry.tenant_id == tid
    assert entry.detail.get("reason") == "sso_sync_deprovision"

    # M-02: an aggregate SSO_SYNCED, so a SCHEDULED run also leaves a trace.
    synced = await _audit_rows(tid, SSO_SYNCED)
    assert len(synced) == 1, "even a scheduled sync must write an SSO_SYNCED aggregate"
    assert synced[0].detail.get("removed") == 1


# --------------------------------------------------------------------------------------- #
# L-03: the last admin of a tenant is never deprovisioned (backstop kicks in).
# --------------------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def tenant_with_one_admin(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """A tenant with ONE SSO admin and two SSO auditors.

    The admin drops out of the desired set (only the two auditors remain). The removal
    ratio (1 of 3, <=50%) would allow the removal -- the last-admin backstop must still
    not carry it out."""
    tid = await _create_tenant(migrated_engine, "ssoaudit-l03")
    async with get_session_factory()() as session:
        await user_repo.create(
            session,
            username="only-admin@ssol03.test",
            password_hash="x",
            role="admin",
            display_name="Only Admin",
            is_sso=True,
            tenant_id=tid,
        )
        for i in (1, 2):
            await user_repo.create(
                session,
                username=f"aud{i}@ssol03.test",
                password_hash="x",
                role="auditor",
                display_name=f"Auditor {i}",
                is_sso=True,
                tenant_id=tid,
            )
    try:
        yield tid
    finally:
        await _cleanup(migrated_engine, tid, "%@ssol03.test")


def _graph_auditors_only() -> MagicMock:
    async def _members(group_id: str) -> list[dict[str, Any]]:
        if group_id == "auditor-gruppe":
            return [
                {"userPrincipalName": "aud1@ssol03.test", "displayName": "Auditor 1"},
                {"userPrincipalName": "aud2@ssol03.test", "displayName": "Auditor 2"},
            ]
        return []  # admin group empty -> the admin drops out of the desired set.

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_members)
    fake.aclose = AsyncMock()
    return fake


async def test_last_admin_is_not_deprovisioned(tenant_with_one_admin: int) -> None:
    tid = tenant_with_one_admin
    with patch.object(oidc, "GraphClient", return_value=_graph_auditors_only()):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, SETTINGS, tenant_id=tid)

    # L-03: the sole admin survives despite the permitted ratio.
    async with get_session_factory()() as session:
        admin = await user_repo.get_by_username(session, "only-admin@ssol03.test")
    assert admin is not None, "the last admin must never be deprovisioned"
    assert admin.role == "admin"

    assert stats["removed"] == 0
    assert stats.get("admin_protected") == 1

    # No USER_DELETED trace for the protected admin.
    deleted = await _audit_rows(tid, USER_DELETED)
    assert deleted == [], "a protected admin must not produce a USER_DELETED entry"
