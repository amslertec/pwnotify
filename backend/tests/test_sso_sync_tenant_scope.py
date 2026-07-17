"""Angriffstest für Task 3 des Multi-Tenant-Feature-Plans (Sicherheitsfix 1).

Hintergrund: `oidc.sync_sso_users` bildete die zu ENTFERNENDE Menge bisher aus
`user_repo.list_sso(session)` -- instanzweit, über ALLE Kunden hinweg. Sobald ein
zweiter SSO-Kunde existiert, würde ein Sync für Kunde A dessen `desired`-Menge (nur A's
Gruppenmitglieder) gegen JEDEN instanzweiten SSO-Benutzer prüfen -- Kunde B's SSO-Konten
(deren UPNs natürlich nicht in A's Gruppen stehen) erschienen dann als "in keiner Gruppe
mehr" und würden gelöscht, INKLUSIVE B's Administratoren. Der Fix scoped sowohl das
Anlegen (`tenant_id` auf dem neuen Konto) als auch die Entfernungsmenge
(`list_sso_for_tenant` statt `list_sso`) auf den synchronisierten Tenant.

Dieser Test ist bewusst NICHT vakuum: er seedet ZWEI Tenants mit je eigenen SSO-Konten
(inkl. je einem SSO-Admin), synchronisiert NUR Tenant A und beweist:
  1. B's SSO-Konten (Admin wie Nicht-Admin) bleiben unverändert -- Rolle, `is_active`,
     Existenz -- exakt der Angriffspfad, den der Bugfix schliesst.
  2. A's eigener veralteter SSO-Benutzer (nicht mehr in A's Soll-Menge) wird weiterhin
     entfernt -- der Fix darf die Kernfunktion (Abgleich) nicht kaputt machen.

Läuft auf echtem Postgres (Port 5433, siehe `conftest.py`). Microsoft Graph wird gemockt
(`patch.object(oidc, "GraphClient", ...)`, Muster aus `test_oidc_group_overage.py`) --
kein Netzwerkzugriff. Seed/Cleanup mit echten Commits, `finally`-Aufräumen für
Wiederholbarkeit des Gesamtlaufs.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from app.db.session import get_session_factory
from app.repositories import user_repo
from app.services import oidc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

SETTINGS: dict[str, Any] = {
    "oidc.enabled": True,
    "oidc.admin_group_id": "admin-gruppe",
    "oidc.auditor_group_id": "",
    "graph.tenant_id": "t",
    "graph.client_id": "c",
    "graph.client_secret": "s",
    "graph.cloud": "global",
}


class _TwoTenantsSso:
    a: int
    b: int


@pytest_asyncio.fixture
async def two_tenants_with_sso_users(
    migrated_engine: AsyncEngine,
) -> AsyncGenerator[_TwoTenantsSso]:
    """Zwei aktive Tenants A/B, jeder mit einem SSO-Admin und einem zweiten SSO-Konto.

    A's zweites Konto (``a-stale``) fehlt bewusst in der später simulierten Soll-Menge
    des Syncs -- es MUSS entfernt werden, damit der Test beweist, dass der Fix die
    normale Abgleichfunktion nicht lahmlegt. B's beide Konten dürfen unter keinen
    Umständen berührt werden.
    """
    async with migrated_engine.connect() as conn:
        a = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sso3TenantA','sso3-tenant-a',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        b = int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        "('Sso3TenantB','sso3-tenant-b',true,now()) RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await conn.commit()

    session_factory = get_session_factory()
    async with session_factory() as session:
        await user_repo.create(
            session,
            username="a-admin@sso3.test",
            password_hash="x",
            role="admin",
            display_name="A Admin",
            is_sso=True,
            tenant_id=a,
        )
        await user_repo.create(
            session,
            username="a-stale@sso3.test",
            password_hash="x",
            role="auditor",
            display_name="A Stale",
            is_sso=True,
            tenant_id=a,
        )
        await user_repo.create(
            session,
            username="b-admin@sso3.test",
            password_hash="x",
            role="admin",
            display_name="B Admin",
            is_sso=True,
            tenant_id=b,
        )
        await user_repo.create(
            session,
            username="b-user@sso3.test",
            password_hash="x",
            role="auditor",
            display_name="B User",
            is_sso=True,
            tenant_id=b,
        )

    tenants = _TwoTenantsSso()
    tenants.a, tenants.b = a, b
    try:
        yield tenants
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM app_user WHERE username LIKE '%@sso3.test'"))
            await conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a, "b": b})
            await conn.commit()


def _fake_graph_client() -> MagicMock:
    """Nur A's Admin-Gruppe hat ein Mitglied -- A's Auditor-Gruppe ist leer, A's `a-stale`
    ist absichtlich in KEINER der beiden Gruppen mehr (soll entfernt werden)."""

    async def _get_group_members(group_id: str) -> list[dict[str, Any]]:
        if group_id == "admin-gruppe":
            return [{"userPrincipalName": "a-admin@sso3.test", "displayName": "A Admin"}]
        return []

    fake = MagicMock()
    fake.get_group_members = AsyncMock(side_effect=_get_group_members)
    fake.aclose = AsyncMock()
    return fake


async def test_sync_for_tenant_a_never_touches_tenant_bs_sso_users(
    two_tenants_with_sso_users: _TwoTenantsSso,
) -> None:
    session_factory = get_session_factory()

    with patch.object(oidc, "GraphClient", return_value=_fake_graph_client()):
        async with session_factory() as session:
            stats = await oidc.sync_sso_users(
                session, SETTINGS, tenant_id=two_tenants_with_sso_users.a
            )

    # Kernbeweis 1 (Angriffspfad geschlossen): B's SSO-Konten -- inklusive B's Admin --
    # existieren unverändert, mit unveränderter Rolle. Vor dem Fix hätte die instanzweite
    # `list_sso()` beide als "nicht mehr in A's Gruppen" gesehen und gelöscht.
    async with session_factory() as session:
        b_admin = await user_repo.get_by_username(session, "b-admin@sso3.test")
        b_user = await user_repo.get_by_username(session, "b-user@sso3.test")
    assert b_admin is not None, "B's SSO-Admin wurde durch A's Sync gelöscht!"
    assert b_admin.role == "admin"
    assert b_admin.is_sso is True
    assert b_user is not None, "B's SSO-Benutzer wurde durch A's Sync gelöscht!"
    assert b_user.role == "auditor"

    # Kernbeweis 2 (Funktion bleibt intakt): A's eigener veralteter SSO-Benutzer wird
    # weiterhin entfernt -- der Fix scoped nur die Menge, er lähmt den Abgleich nicht.
    async with session_factory() as session:
        a_stale = await user_repo.get_by_username(session, "a-stale@sso3.test")
        a_admin = await user_repo.get_by_username(session, "a-admin@sso3.test")
    assert a_stale is None, "A's veralteter SSO-Benutzer hätte entfernt werden müssen."
    assert a_admin is not None
    assert a_admin.role == "admin"

    assert stats["synced"] == 1
    assert stats["removed"] == 1
    assert not stats.get("removal_blocked")
