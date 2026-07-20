"""Central constants for the row-level-security isolation (Phase 2)."""

from __future__ import annotations

# Restricted DB role that runtime sessions switch into via SET LOCAL ROLE.
# NON-superuser, NON-BYPASSRLS — only that way does RLS take effect (owner/superuser bypasses it).
APP_ROLE = "pwnotify_app"

# Session GUC from which the RLS policies read the active tenant.
TENANT_GUC = "app.current_tenant"

# Tables with tenant isolation (all carry tenant_id).
RLS_TABLES = (
    "entra_user",
    "exclusion",
    "notification_log",
    "run",
    "setting",
    "audit_log",
)

# Migration authors (finding F-07): new tables no longer inherit any privileges for
# `pwnotify_app` (default privileges were revoked in migration f8a9b0c1d2e3). A `create_table`
# migration whose table is read/written under a tenant-scoped session MUST grant privileges
# and set up RLS explicitly, or runtime writes fail with "permission denied". An owner-only
# table (auth, sessions, assignments) grants nothing. See `backend/alembic/README.md` for the
# full rule; `tests/test_rls_policies.py` (table + sequence grant-soll) enforces it.
