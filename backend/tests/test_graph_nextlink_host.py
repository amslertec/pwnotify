"""I3: a paginating Graph call must never follow an `@odata.nextLink` to a foreign host.

Every page request carries the app's Bearer token. `@odata.nextLink` is an absolute URL
taken verbatim from the response body; if a compromised/spoofed response (or a broken proxy)
returned a link pointing at an attacker-controlled host, the loop would happily send the
Bearer token there. Graph's response is TLS-authenticated so this is defense-in-depth, but
the failure mode (token disclosure off-tenant) is severe and the check is cheap.

`get_group_members` stands in for the shared behaviour of all paginating methods (they route
their nextLink through the same `_next_link` helper). Two facts are proven:
  * a nextLink on a FOREIGN host is NOT followed (loop stops after the first page); RED
    against the pre-fix code, which would follow it and issue a second request.
  * a nextLink on the SAME Graph host IS followed normally (one extra request), so the fix
    does not break legitimate pagination.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.graph import client as gc

CFG = gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s", cloud="global")


@pytest.fixture
def graph() -> gc.GraphClient:
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()):
        return gc.GraphClient(CFG)


def _two_page_stub(second_page_next_link: str) -> AsyncMock:
    """First page carries `second_page_next_link`; the (would-be) second page has none."""
    call_count = 0

    async def _respond(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body: dict[str, object] = {"value": [{"id": f"user-{call_count}"}]}
        if call_count == 1:
            body["@odata.nextLink"] = second_page_next_link
        return httpx.Response(200, json=body)

    return AsyncMock(side_effect=_respond)


@pytest.mark.asyncio
async def test_foreign_host_nextlink_is_not_followed(graph: gc.GraphClient) -> None:
    fake_request = _two_page_stub("https://evil.example.com/v1.0/groups/g/transitiveMembers?page=2")
    with patch.object(graph, "_request", fake_request):
        members = await graph.get_group_members("g")

    # Loop must stop after the first page -- the Bearer token is never sent to evil.example.com.
    assert fake_request.await_count == 1
    assert len(members) == 1


@pytest.mark.asyncio
async def test_same_host_nextlink_is_followed(graph: gc.GraphClient) -> None:
    fake_request = _two_page_stub(
        "https://graph.microsoft.com/v1.0/groups/g/transitiveMembers?$skiptoken=abc"
    )
    with patch.object(graph, "_request", fake_request):
        members = await graph.get_group_members("g")

    assert fake_request.await_count == 2
    assert len(members) == 2
