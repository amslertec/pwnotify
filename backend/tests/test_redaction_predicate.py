"""Finding I1: a single 'is this a secret key' predicate shared by the audit trail and the
structured logger, so their redaction can never drift apart.

Before the fix, `services.audit._clean` matched secret keys by EXACT name against a small
set, while `core.logging` additionally used substring matching. A future
`detail={"new_password": ...}` or `"totp_secret"` therefore slipped through the audit
redaction (exact match missed it) and landed in the admin-readable, exportable audit log.

These tests assert the shared predicate redacts strictly MORE than the old audit set (the
union) while never over-redacting known non-secret keys such as `country_code`/`error_code`.
RED before the fix: `new_password`/`totp_secret` stayed in the audit `_clean` output.
"""

from __future__ import annotations

from app.core.redaction import is_secret_key
from app.services.audit import _clean


def test_is_secret_key_substring_and_exact() -> None:
    # Substring markers -- any key containing these is secret.
    for key in ("new_password", "current_password", "totp_secret", "refresh_token", "id_token"):
        assert is_secret_key(key), key
    # Exact-only markers preserved from the previous sets (not caught by substrings).
    for key in (
        "code",
        "recovery_codes",
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "private_key",
    ):
        assert is_secret_key(key), key


def test_is_secret_key_does_not_over_redact_code_suffixed_keys() -> None:
    # `code` is exact-only on purpose: a substring rule would nuke these legitimate keys.
    for key in ("country_code", "error_code", "status_code", "reason_code"):
        assert not is_secret_key(key), key
    for key in ("count", "reason", "note", "format", "search", "status", "excluded"):
        assert not is_secret_key(key), key


def test_is_secret_key_is_case_insensitive() -> None:
    assert is_secret_key("New_Password")
    assert is_secret_key("Authorization")


def test_audit_clean_drops_drifted_secret_keys() -> None:
    """The concrete drift scenario: audit `_clean` must now drop `new_password`/`totp_secret`
    while keeping non-secret keys. RED against the old exact-match code (both stayed)."""
    out = _clean({"new_password": "x", "totp_secret": "y", "country_code": "CH", "count": 3})
    assert "new_password" not in out
    assert "totp_secret" not in out
    assert out["country_code"] == "CH"
    assert out["count"] == 3


def test_audit_clean_drops_nested_drifted_secret_key() -> None:
    out = _clean({"reason": "manual", "nested": {"refresh_token": "z", "note": "kept"}})
    assert "refresh_token" not in out["nested"]
    assert out["nested"]["note"] == "kept"
