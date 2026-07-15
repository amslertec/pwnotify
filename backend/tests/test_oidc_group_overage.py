"""SSO-Anmeldung, wenn das Token keine Gruppenliste enthält.

Ist ein Benutzer in mehr als 200 Gruppen, liefert Entra im ID-Token statt der Liste nur
einen Verweis ("Overage"). Bisher wurde die Anmeldung dann pauschal abgelehnt — was
ausgerechnet Konten mit vielen Mitgliedschaften trifft, also typischerweise die
Administratoren eines grossen Tenants. Jetzt wird gezielt bei Graph nachgefragt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services import oidc

SETTINGS = {
    "graph.tenant_id": "t",
    "graph.client_id": "c",
    "graph.client_secret": "s",
    "graph.cloud": "global",
    "oidc.admin_group_id": "admin-gruppe",
    "oidc.auditor_group_id": "auditor-gruppe",
}


@pytest.mark.asyncio
async def test_graph_lookup_finds_admin_membership() -> None:
    fake = MagicMock()
    fake.check_member_groups = AsyncMock(return_value={"admin-gruppe"})
    fake.aclose = AsyncMock()
    with patch.object(oidc, "GraphClient", return_value=fake):
        groups = await oidc._groups_via_graph(
            SETTINGS, "user-oid", ["admin-gruppe", "auditor-gruppe"]
        )
    assert groups == ["admin-gruppe"]
    fake.aclose.assert_awaited()  # Verbindung muss auch hier geschlossen werden


@pytest.mark.asyncio
async def test_graph_error_denies_instead_of_granting() -> None:
    """Im Zweifel keine Rechte vergeben — lieber abgelehnt als fälschlich Admin."""
    fake = MagicMock()
    fake.check_member_groups = AsyncMock(side_effect=RuntimeError("Graph down"))
    fake.aclose = AsyncMock()
    with patch.object(oidc, "GraphClient", return_value=fake):
        assert await oidc._groups_via_graph(SETTINGS, "user-oid", ["admin-gruppe"]) is None
    fake.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_no_secret_means_no_lookup() -> None:
    ohne = {**SETTINGS, "graph.client_secret": ""}
    assert await oidc._groups_via_graph(ohne, "user-oid", ["admin-gruppe"]) is None


@pytest.mark.asyncio
async def test_no_groups_configured_means_no_lookup() -> None:
    assert await oidc._groups_via_graph(SETTINGS, "user-oid", ["", ""]) is None


@pytest.mark.asyncio
async def test_no_user_id_means_no_lookup() -> None:
    assert await oidc._groups_via_graph(SETTINGS, "", ["admin-gruppe"]) is None
