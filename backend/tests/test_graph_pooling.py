"""HTTP-Verbindungs-Pooling im Graph-Client.

`send_mail` öffnete pro Mail einen eigenen httpx-Client und damit eine neue TCP-/TLS-
Verbindung. Gegen den echten Graph-Endpunkt gemessen: rund 26 ms je Aufruf allein für den
Verbindungsaufbau (29 ms ohne, 4 ms mit Pooling). Beim Massenversand summiert sich das.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.services.graph import client as gc

CFG = gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s", cloud="global")


@pytest.fixture
def graph() -> gc.GraphClient:
    # MSAL prüft die Authority beim Erzeugen über das Netz — hier irrelevant.
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()):
        return gc.GraphClient(CFG)


def test_reuses_the_same_client(graph: gc.GraphClient) -> None:
    assert graph._shared_http() is graph._shared_http()


@pytest.mark.asyncio
async def test_aclose_closes_the_connection(graph: gc.GraphClient) -> None:
    c = graph._shared_http()
    await graph.aclose()
    assert c.is_closed


@pytest.mark.asyncio
async def test_new_client_after_close(graph: gc.GraphClient) -> None:
    """Nach dem Schliessen darf kein toter Client zurückkommen."""
    alt = graph._shared_http()
    await graph.aclose()
    neu = graph._shared_http()
    assert neu is not alt
    assert not neu.is_closed
    await graph.aclose()


@pytest.mark.asyncio
async def test_aclose_is_idempotent(graph: gc.GraphClient) -> None:
    """Der Runner schliesst im finally — das darf auch ohne offenen Client laufen."""
    await graph.aclose()
    await graph.aclose()


@pytest.mark.asyncio
async def test_context_manager_closes() -> None:
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()):
        async with gc.GraphClient(CFG) as g:
            c = g._shared_http()
    assert c.is_closed
