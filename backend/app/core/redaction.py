"""Single source of truth for 'is this a secret key' -- used by both the structured logger
(`core.logging`) and the audit trail (`services.audit`) so their redaction can never drift
apart (finding I1).

Before this module, the audit trail matched secret keys by EXACT name against a small set
while the logger additionally used substring matching. A future `detail={"new_password": ...}`
or `"totp_secret"` therefore slipped past the audit redaction and landed in the
admin-readable, exportable audit log. Both callers now share `is_secret_key`, which is the
UNION of the two previous rule sets -- it redacts strictly MORE than either did alone, never
less.
"""

from __future__ import annotations

# Exact keys to redact. Deliberately limited to keys NOT already caught by the substring
# markers below, so this set stays the minimal union of the two previous exact sets:
#   - `code`: TOTP/one-time (and OIDC auth) codes -- exact ONLY, because a substring rule
#     would also nuke legitimate keys like `country_code`/`error_code`/`status_code`.
#   - `recovery_codes`, `authorization`, `cookie`, `set-cookie`, `api_key`, `private_key`:
#     carried over from the logger's exact set; none contain a substring marker.
_SECRET_KEYS_EXACT = frozenset(
    {
        "code",
        "recovery_codes",
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "private_key",
    }
)

# Substring markers: any key CONTAINING one of these is secret -- covers `new_password`,
# `current_password`, `smtp_password`, `totp_secret`, `client_secret`, `graph_client_secret`,
# `secret_key`, `access_token`, `refresh_token`, `id_token`, ... without enumerating each.
# `code` is intentionally NOT here to avoid over-redacting `*_code` keys.
_SECRET_KEY_SUBSTRINGS = ("password", "secret", "token")


def is_secret_key(key: str) -> bool:
    """True if ``key`` names a value that must never be stored/logged in cleartext."""
    k = key.lower()
    return k in _SECRET_KEYS_EXACT or any(s in k for s in _SECRET_KEY_SUBSTRINGS)
