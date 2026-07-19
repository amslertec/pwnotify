"""Recursive, broadened log redaction (I2).

``_redact`` used to walk only the top level of the event dict with an exact
lowercase key match, so secrets nested in a ``detail={...}`` sub-dict or a list of
dicts (and several sensitive keys such as ``totp_secret``/``recovery_codes``) were
logged in cleartext. These tests build a nested event dict covering all of those
gaps and assert every secret is scrubbed at every depth, while a PII key stays
masked (not redacted) and an unrelated key is left untouched.
"""

from __future__ import annotations

from app.core.logging import _redact
from app.services.audit import _clean


def test_secrets_are_redacted_at_every_depth() -> None:
    event_dict = {
        "event": "graph_sync_failed",
        "new_password": "hunter2",
        "totp_secret": "ABCDEF",
        "graph_client_secret": "cs-value",
        "id_token": "jwt-value",
        "detail": {
            "recovery_codes": ["a", "b"],
            "password": "p",
        },
        "items": [{"client_secret": "z"}],
        "upn": "pascal.amsler@example.com",
    }

    out = _redact(None, "", event_dict)

    assert out["new_password"] == "***redacted***"
    assert out["totp_secret"] == "***redacted***"
    assert out["graph_client_secret"] == "***redacted***"
    assert out["id_token"] == "***redacted***"
    assert out["detail"]["recovery_codes"] == "***redacted***"
    assert out["detail"]["password"] == "***redacted***"
    assert out["items"][0]["client_secret"] == "***redacted***"


def test_pii_key_is_masked_not_redacted() -> None:
    """PII keys stay distinct from secret keys -- masked (domain kept), not dropped."""
    out = _redact(None, "", {"upn": "pascal.amsler@example.com"})
    assert out["upn"] == "pa***@example.com"


def test_unrelated_key_is_untouched() -> None:
    out = _redact(None, "", {"event": "run_done"})
    assert out["event"] == "run_done"


def test_audit_clean_drops_nested_secret_key() -> None:
    """Secondary hardening: `services.audit._clean` also recurses into a nested
    `detail` sub-dict so a secret key buried there is dropped, not just at the
    top level."""
    out = _clean({"reason": "manual", "nested": {"client_secret": "z", "note": "kept"}})
    assert "client_secret" not in out["nested"]
    assert out["nested"]["note"] == "kept"
