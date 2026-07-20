"""Small helpers for reading request metadata safely into the database.

Kept separate from the route modules so every caller truncates the same way.
"""

from __future__ import annotations

from fastapi import Request

# Width of the `user_agent` columns on `audit_log` and `user_session` (both
# `Column(String(400))`). Postgres REJECTS an over-long varchar with
# `value too long for type character varying(400)` -- it does NOT truncate -- so an
# unbounded `User-Agent` header would fail the INSERT and roll back the surrounding
# transaction. In the login handler that transaction also carries the failed-attempt
# counter and the audit rows, so an attacker could send a 500-char header to suppress the
# account lockout AND the audit trail (H1). Truncating at read time closes that at the
# root: the INSERT can no longer fail on this field, so splitting the transaction (a
# separate commit for the counter) is deliberately NOT done -- it would only add
# complexity and new failure modes (YAGNI).
_USER_AGENT_MAX_LEN = 400


def client_user_agent(request: Request | None) -> str | None:
    """Return the request `User-Agent`, truncated to the DB column width (or `None`).

    `None` request or a missing/empty header both yield `None` so callers can pass the
    result straight into a nullable column.
    """
    if request is None:
        return None
    return (request.headers.get("user-agent") or "")[:_USER_AGENT_MAX_LEN] or None
