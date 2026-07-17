"""Aktiver-Mandant-Claim im Access-Token (Phase 4a, Task 1).

`issue_token_pair` bekommt einen optionalen `active_tenant`-Parameter: Ist er gesetzt,
landet er als `active_tenant`-Claim im ACCESS-Token (spätere Tasks lesen ihn dort für
Autorisierung). Der REFRESH-Token bleibt bewusst ohne diesen Claim -- er überlebt eine
Mandanten-Umschaltung unverändert und trägt nur `sub`+`jti`. Ist kein `active_tenant`
übergeben (Alt-Verhalten), taucht der Claim gar nicht erst im Payload auf -- rückwärts-
kompatibel zu allem, was Tokens ohne diesen Claim erwartet.
"""

from __future__ import annotations

from app.core.security import decode_token, issue_token_pair


def test_active_tenant_claim_present_in_access_token_only() -> None:
    pair = issue_token_pair("1", active_tenant=5)

    access_payload = decode_token(pair.access_token, expected_type="access")
    assert access_payload["active_tenant"] == 5

    refresh_payload = decode_token(pair.refresh_token, expected_type="refresh")
    assert "active_tenant" not in refresh_payload


def test_no_active_tenant_claim_when_not_given() -> None:
    """Rückwärtskompatibel: ohne `active_tenant` bleibt der Access-Token wie bisher."""
    pair = issue_token_pair("1")

    access_payload = decode_token(pair.access_token, expected_type="access")
    assert "active_tenant" not in access_payload
