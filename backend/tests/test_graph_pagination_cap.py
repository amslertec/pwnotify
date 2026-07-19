"""L6: Graph pagination loops must not follow an unbounded `@odata.nextLink` chain forever.

A hostile or broken Graph endpoint (or a misbehaving proxy) could keep returning a fresh
`@odata.nextLink` on every page, turning a `while url:` loop into an effectively infinite
fetch. `get_group_member_ids` is the simplest paginating method to drive (returns a plain
`set`, no generator machinery) so it stands in for the shared cap behaviour of all five
paginating methods.

Non-vacuous: the stubbed `_request` returns a page with a *fresh* `@odata.nextLink` far
past any sane cap (`_STUB_CHAIN_LENGTH` pages, well above the `1000` the plan calls for) --
without a page cap the loop would follow the whole chain. A wall-clock timeout is
deliberately NOT the proof mechanism (that would make this test flaky/slow); the stub chain
is bounded so this test always terminates quickly, and the hard call-count assertion is
what actually proves whether the cap fired.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.graph import client as gc

CFG = gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s", cloud="global")

# Deliberately well above the expected page cap (plan calls for 1000) so an uncapped loop
# would run far past it -- but still finite, so a RED run (no cap implemented yet) actually
# terminates instead of hanging.
_STUB_CHAIN_LENGTH = 2500


@pytest.fixture
def graph() -> gc.GraphClient:
    # MSAL validates the authority over the network on construction -- irrelevant here.
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()):
        return gc.GraphClient(CFG)


def _make_bounded_chain_stub() -> AsyncMock:
    """Each call answers with a `@odata.nextLink` for `_STUB_CHAIN_LENGTH` pages, then a
    final page without one -- long enough to prove an unbounded chain gets cut off well
    before its end, short enough to keep a pre-fix (uncapped) test run fast."""
    call_count = 0

    async def _respond(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body: dict[str, object] = {"value": [{"id": f"user-{call_count}"}]}
        if call_count < _STUB_CHAIN_LENGTH:
            body["@odata.nextLink"] = (
                f"https://graph.microsoft.com/v1.0/groups/g/transitiveMembers?page={call_count}"
            )
        return httpx.Response(200, json=body)

    return AsyncMock(side_effect=_respond)


@pytest.mark.asyncio
async def test_get_group_member_ids_stops_at_page_cap(graph: gc.GraphClient) -> None:
    fake_request = _make_bounded_chain_stub()
    with patch.object(graph, "_request", fake_request):
        ids = await graph.get_group_member_ids("g")

    assert fake_request.await_count == gc._MAX_PAGES
    assert len(ids) == gc._MAX_PAGES
