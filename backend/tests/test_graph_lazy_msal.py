"""A partially configured tenant must not crash just by building the Graph client.

Prod (run_id 19, tenant 2): the mail backend defaults to ``graph``, so the scheduled
run builds a ``GraphClient`` even when there is nothing to send. With an empty
``graph.tenant_id`` the authority degrades to ``https://login.microsoftonline.com/``
(no tenant path segment); MSAL rejected that *at construction time*, turning a harmless
no-op run into ``status=error``. The client must therefore build MSAL lazily — only when
a token is actually requested.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.core.errors import GraphError
from app.services.graph import client as gc
from app.services.mail import build_sender


def test_construction_with_empty_tenant_does_not_raise() -> None:
    """The exact prod trigger: empty tenant_id -> invalid authority, but no eager MSAL."""
    client = gc.GraphClient(gc.GraphConfig(tenant_id="", client_id="x", client_secret="y"))
    # No MSAL app built until a token is requested.
    assert client._app is None


def test_build_graph_sender_with_empty_tenant_does_not_raise() -> None:
    """build_sender defaults to the graph backend — building it must stay harmless."""
    sender = build_sender({"mail.backend": "graph", "graph.tenant_id": "", "mail.from": "a@b.ch"})
    assert sender is not None


def test_msal_app_is_built_once_on_first_use() -> None:
    """A valid config yields a memoised MSAL app, built only when first requested."""
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()) as ctor:
        client = gc.GraphClient(gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s"))
        ctor.assert_not_called()  # not built eagerly in __init__
        app = client._msal_app()
        assert client._msal_app() is app
        ctor.assert_called_once()  # and cached, not rebuilt


def test_blank_config_fast_fails_as_grapherror_without_building_msal() -> None:
    """A9/A10: whitespace tenant_id -> GraphError(graph_not_configured), no MSAL, no network.

    Otherwise a blank/whitespace tenant_id would fail only after MSAL's instance-discovery
    network roundtrip, repeated per recipient in a send loop (a self-DoS). The check is cheap
    and deterministic, so it must never construct MSAL nor hit the network.
    """
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()) as ctor:
        client = gc.GraphClient(gc.GraphConfig(tenant_id="  ", client_id="x", client_secret="y"))
        with pytest.raises(GraphError) as exc:
            client._msal_app()
        assert exc.value.code == "graph_not_configured"
        ctor.assert_not_called()  # no MSAL constructed -> no discovery roundtrip


@pytest.mark.asyncio
async def test_acquire_token_on_blank_config_raises_grapherror() -> None:
    """_acquire_token surfaces GraphError (not MSAL's ValueError) on a blank config."""
    with patch.object(gc.msal, "ConfidentialClientApplication", MagicMock()) as ctor:
        client = gc.GraphClient(gc.GraphConfig(tenant_id="", client_id="c", client_secret="s"))
        with pytest.raises(GraphError) as exc:
            await client._acquire_token()
        assert exc.value.code == "graph_not_configured"
        ctor.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_on_blank_config_is_clean_not_500() -> None:
    """test_connection catches the GraphError -> connected=False, no ValueError/500 leak."""
    client = gc.GraphClient(gc.GraphConfig(tenant_id=" ", client_id="c", client_secret="s"))
    result = await client.test_connection()
    assert result.connected is False
    assert result.error


def test_msal_valueerror_is_wrapped_as_grapherror() -> None:
    """A10: if MSAL itself raises ValueError on construction, surface a GraphError."""
    with patch.object(
        gc.msal, "ConfidentialClientApplication", MagicMock(side_effect=ValueError("bad authority"))
    ):
        client = gc.GraphClient(gc.GraphConfig(tenant_id="t", client_id="c", client_secret="s"))
        with pytest.raises(GraphError) as exc:
            client._msal_app()
        assert exc.value.code == "graph_config_invalid"
