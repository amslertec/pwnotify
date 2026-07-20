"""Audit + Lockout-Backstop des geplanten SSO-Syncs (Befunde M-02, L-03).

Zwei Eigenschaften des `oidc.sync_sso_users`-Deprovision-Passes, die der frühere Code
nicht hatte:

* **M-02 (Löschen ist nie leise):** Ein geplanter/direkter Sync, der ein SSO-Konto
  entfernt, MUSS pro entferntem Konto einen `USER_DELETED`-Eintrag (actor_type `system`,
  Ziel-UPN, Tenant des Syncs) schreiben und den Lauf insgesamt mit einem aggregierten
  `SSO_SYNCED` protokollieren. Vorher schrieb nur die manuelle Route einen Sammel-Eintrag;
  der geplante Runner-Pfad löschte still. Gegen den alten Code ist der USER_DELETED-Teil rot.

* **L-03 (Last-Admin-Backstop):** Fällt der EINZIGE Admin eines Tenants aus der Gruppe,
  während die Löschquote (<=50 %) die Entfernung erlauben würde, darf er NICHT entfernt
  werden -- konsistent mit dem A4-Backstop in `set_role`/`delete_user`. Gegen den alten
  Code, der nur `removal_blocked_reason` kannte, wird der letzte Admin gelöscht (rot).

Läuft auf echtem Postgres (Port 5433, siehe `conftest.py`); Microsoft Graph ist gemockt
(`patch.object(oidc, "GraphClient", ...)`). Seed/Cleanup mit echten Commits.
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
        # audit_log.tenant_id + app_user.tenant_id sind FKs auf tenant -- vor dem Tenant löschen.
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
# M-02: eine Deprovision-Löschung wird auditiert (USER_DELETED je Konto + Aggregat).
# --------------------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def tenant_with_stale_sso(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Ein Tenant mit einem bleibenden SSO-Admin und einem veralteten SSO-Auditor.

    Der Auditor (`stale`) fehlt in der simulierten Soll-Menge -> er muss entfernt und die
    Entfernung auditiert werden. Der Admin bleibt (steht in der Admin-Gruppe)."""
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

    # M-02: genau ein USER_DELETED, system-attributiert, Ziel-UPN, Tenant des Syncs.
    deleted = await _audit_rows(tid, USER_DELETED)
    assert len(deleted) == 1, "Deprovision-Löschung muss genau einen USER_DELETED schreiben"
    entry = deleted[0]
    assert entry.actor_type == "system"
    assert entry.target == "stale-auditor@ssoaudit.test"
    assert entry.tenant_id == tid
    assert entry.detail.get("reason") == "sso_sync_deprovision"

    # M-02: ein Aggregat-SSO_SYNCED, damit auch ein GEPLANTER Lauf eine Spur hinterlässt.
    synced = await _audit_rows(tid, SSO_SYNCED)
    assert len(synced) == 1, "Auch ein geplanter Sync muss ein SSO_SYNCED-Aggregat schreiben"
    assert synced[0].detail.get("removed") == 1


# --------------------------------------------------------------------------------------- #
# L-03: der letzte Admin eines Tenants wird nie deprovisioniert (Backstop schlägt zu).
# --------------------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def tenant_with_one_admin(migrated_engine: AsyncEngine) -> AsyncGenerator[int]:
    """Ein Tenant mit EINEM SSO-Admin und zwei SSO-Auditoren.

    Der Admin fällt aus der Soll-Menge (nur die zwei Auditoren bleiben). Die Löschquote
    (1 von 3, <=50 %) würde die Entfernung zulassen -- der Last-Admin-Backstop darf sie
    trotzdem nicht ausführen."""
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
        return []  # Admin-Gruppe leer -> der Admin fällt aus der Soll-Menge.

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_members)
    fake.aclose = AsyncMock()
    return fake


async def test_last_admin_is_not_deprovisioned(tenant_with_one_admin: int) -> None:
    tid = tenant_with_one_admin
    with patch.object(oidc, "GraphClient", return_value=_graph_auditors_only()):
        async with get_session_factory()() as session:
            stats = await oidc.sync_sso_users(session, SETTINGS, tenant_id=tid)

    # L-03: der einzige Admin überlebt trotz erlaubter Quote.
    async with get_session_factory()() as session:
        admin = await user_repo.get_by_username(session, "only-admin@ssol03.test")
    assert admin is not None, "Der letzte Admin darf nie deprovisioniert werden"
    assert admin.role == "admin"

    assert stats["removed"] == 0
    assert stats.get("admin_protected") == 1

    # Keine USER_DELETED-Spur für den geschützten Admin.
    deleted = await _audit_rows(tid, USER_DELETED)
    assert deleted == [], "Ein geschützter Admin darf keinen USER_DELETED-Eintrag erzeugen"
