"""`get_group_members` muss über `@odata.nextLink` paginieren (Grundlage für den
Gruppen-Mitglieder-Snapshot: id -> entra_id, userPrincipalName -> upn, displayName,
mail). Fake-Transport statt echtem Graph — kein Netzzugriff nötig.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.graph import client as gc

CFG = gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s", cloud="global")

PAGE_1 = {
    "value": [
        {
            "id": "user-1",
            "userPrincipalName": "alice@example.com",
            "displayName": "Alice",
            "mail": "alice@example.com",
            "accountEnabled": True,
        }
    ],
    "@odata.nextLink": "https://graph.microsoft.com/v1.0/groups/g/transitiveMembers/next-page",
}

PAGE_2 = {
    "value": [
        {
            "id": "user-2",
            "userPrincipalName": "bob@example.com",
            "displayName": "Bob",
            "mail": "bob@example.com",
            "accountEnabled": True,
        }
    ],
}


@pytest.fixture
def graph() -> gc.GraphClient:
    # MSAL prüft die Authority beim Erzeugen über das Netz — hier irrelevant.
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()):
        return gc.GraphClient(CFG)


@pytest.mark.asyncio
async def test_get_group_members_concatenates_both_pages(graph: gc.GraphClient) -> None:
    responses = [
        httpx.Response(200, json=PAGE_1),
        httpx.Response(200, json=PAGE_2),
    ]
    fake_request = AsyncMock(side_effect=responses)
    with patch.object(graph, "_request", fake_request):
        members = await graph.get_group_members("g")

    assert fake_request.await_count == 2
    assert [m["id"] for m in members] == ["user-1", "user-2"]
    for member in members:
        assert {"id", "userPrincipalName", "displayName", "mail"} <= member.keys()
    assert members[0]["userPrincipalName"] == "alice@example.com"
    assert members[0]["displayName"] == "Alice"
    assert members[1]["mail"] == "bob@example.com"

    # Erste Anfrage folgt der transitiveMembers/microsoft.graph.user-Route, die zweite
    # dem @odata.nextLink der ersten Seite.
    first_call_url = fake_request.await_args_list[0].args[2]
    assert "transitiveMembers/microsoft.graph.user" in first_call_url
    second_call_url = fake_request.await_args_list[1].args[2]
    assert second_call_url == PAGE_1["@odata.nextLink"]


@pytest.mark.asyncio
async def test_get_group_members_empty_group_returns_empty_list(graph: gc.GraphClient) -> None:
    fake_request = AsyncMock(return_value=httpx.Response(200, json={"value": []}))
    with patch.object(graph, "_request", fake_request):
        members = await graph.get_group_members("empty-group")

    assert members == []
    assert fake_request.await_count == 1
