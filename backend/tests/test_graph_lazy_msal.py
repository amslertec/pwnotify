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
