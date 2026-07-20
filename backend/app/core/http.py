"""Small helpers for reading request metadata safely into the database.

Kept separate from the route modules so every caller truncates the same way.
"""

from __future__ import annotations

from fastapi import Request

# Every request-fed metadata field (User-Agent, client IP, ...) that lands in a NARROWER DB
# column is truncated here at read time. Postgres REJECTS an over-long varchar with
# `value too long for type character varying(N)` -- it does NOT truncate -- so an unbounded
# value would fail the INSERT and roll back the surrounding transaction. In the login handler
# that transaction also carries the failed-attempt counter and the audit rows, so an attacker
# could send an over-long value to suppress the account lockout AND the audit trail (H1, and
# the sister finding F-01 over the client IP). Truncating at read time closes that at the root:
# the INSERT can no longer fail on these fields, so splitting the transaction (a separate commit
# for the counter) is deliberately NOT done -- it would only add complexity and new failure
# modes (YAGNI). Column widths, both mirrored on `audit_log` and `user_session`:
#   - `user_agent`  -> `String(400)`
#   - `ip_address`  -> `String(64)`
_USER_AGENT_MAX_LEN = 400
_IP_MAX_LEN = 64


def client_user_agent(request: Request | None) -> str | None:
    """Return the request `User-Agent`, truncated to the DB column width (or `None`).

    `None` request or a missing/empty header both yield `None` so callers can pass the
    result straight into a nullable column.
    """
    if request is None:
        return None
    return (request.headers.get("user-agent") or "")[:_USER_AGENT_MAX_LEN] or None


def client_ip(request: Request | None) -> str | None:
    """Return the request client host, truncated to the DB column width (or `None`).

    Same rationale as `client_user_agent`: with a trusted proxy configured, uvicorn's
    ProxyHeaders middleware overwrites `request.client` from `X-Forwarded-For` WITHOUT
    validating it is an IP, so an attacker-controlled, arbitrarily long value would fail the
    `varchar(64)` INSERT and roll back the shared lockout+audit transaction (finding F-01,
    same class as H1). `None` request/client or an empty host all yield `None` so callers can
    pass the result straight into a nullable column.
    """
    if request is None or request.client is None:
        return None
    return (request.client.host or "")[:_IP_MAX_LEN] or None
