"""A2: a paginating Graph call must hard-abort on an untrusted `@odata.nextLink`.

Every page request carries the app's Bearer token. `@odata.nextLink` is an absolute URL
taken verbatim from the response body; if a compromised/spoofed response (or a broken proxy)
returned a link pointing at an attacker-controlled host -- or downgraded the SAME host to
plain `http://` -- the loop would send the Bearer token there (in the http case, in the
clear). Graph's response is TLS-authenticated so this is defense-in-depth, but the failure
mode (an app token for User.Read.All/Domain.Read.All/Mail.Send leaking off-tenant) is severe,
so a mismatching nextLink is treated as a security event: the call raises `GraphError`, it is
NOT silently dropped (which would quietly truncate the result set and mask the tampering).

`get_group_members` stands in for the shared behaviour of all paginating methods (they route
their nextLink through the same `_next_link` helper). Proven here:
  * `_same_graph_host` requires BOTH host AND https scheme.
  * a nextLink on a FOREIGN host raises and issues no second request.
  * a nextLink on the SAME host but `http://` (scheme downgrade) raises and issues no second
    request -- the Bearer token never rides a cleartext connection. RED against the pre-fix
    code, which compared only the netloc and would follow it.
  * a nextLink on the SAME Graph host over https IS followed normally (one extra request), so
    legitimate pagination still works.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.core.errors import GraphError
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


def test_same_graph_host_requires_https_scheme(graph: gc.GraphClient) -> None:
    assert graph._same_graph_host("https://graph.microsoft.com/v1.0/users") is True
    # Same host, wrong scheme -- a cleartext downgrade must not pass.
    assert graph._same_graph_host("http://graph.microsoft.com/v1.0/users") is False
    # Foreign host.
    assert graph._same_graph_host("https://evil.example.com/v1.0/users") is False


@pytest.mark.asyncio
async def test_foreign_host_nextlink_aborts(graph: gc.GraphClient) -> None:
    fake_request = _two_page_stub("https://evil.example.com/v1.0/groups/g/transitiveMembers?page=2")
    with patch.object(graph, "_request", fake_request), pytest.raises(GraphError) as exc:
        await graph.get_group_members("g")

    assert exc.value.code == "graph_nextlink_untrusted"
    # The Bearer token is never sent to evil.example.com: no second request is issued.
    assert fake_request.await_count == 1


@pytest.mark.asyncio
async def test_http_same_host_nextlink_aborts(graph: gc.GraphClient) -> None:
    # Same host, but downgraded to cleartext http:// -- the token must not ride that link.
    fake_request = _two_page_stub("http://graph.microsoft.com/v1.0/groups/g/transitiveMembers?p=2")
    with patch.object(graph, "_request", fake_request), pytest.raises(GraphError) as exc:
        await graph.get_group_members("g")

    assert exc.value.code == "graph_nextlink_untrusted"
    assert fake_request.await_count == 1


@pytest.mark.asyncio
async def test_same_host_https_nextlink_is_followed(graph: gc.GraphClient) -> None:
    fake_request = _two_page_stub(
        "https://graph.microsoft.com/v1.0/groups/g/transitiveMembers?$skiptoken=abc"
    )
    with patch.object(graph, "_request", fake_request):
        members = await graph.get_group_members("g")

    assert fake_request.await_count == 2
    assert len(members) == 2
