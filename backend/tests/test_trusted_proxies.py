"""Absicherung gegen den Rate-Limit-Bypass über gefälschtes ``X-Forwarded-For``.

Rate-Limit und Login-Lockout schlüsseln auf die Client-IP auf. Vertraut Uvicorn dem
``X-Forwarded-For``-Header pauschal, setzt ein Angreifer ihn selbst und umgeht beide
Schutzmechanismen — reproduziert mit rotierendem Header: 15/15 Logins ohne 429.
"""

from __future__ import annotations

import inspect

from app.core.config import Settings


def test_trusted_proxies_default_is_not_wildcard() -> None:
    """Der Default darf niemals allen Quellen X-Forwarded-For glauben."""
    assert Settings().trusted_proxies != "*"


def test_trusted_proxies_default_is_loopback() -> None:
    assert Settings().trusted_proxies == "127.0.0.1"


def test_trusted_proxies_is_configurable() -> None:
    """Hinter einem Reverse-Proxy muss dessen Netz eintragbar sein."""
    assert Settings(trusted_proxies="172.18.0.0/16").trusted_proxies == "172.18.0.0/16"


def test_entrypoint_does_not_hardcode_wildcard() -> None:
    """forwarded_allow_ips muss aus den Settings kommen, nicht fest "*" sein."""
    from app import entrypoint

    src = inspect.getsource(entrypoint.main)
    assert 'forwarded_allow_ips="*"' not in src
    assert "forwarded_allow_ips=settings.trusted_proxies" in src
