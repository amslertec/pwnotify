"""revoke default privileges for pwnotify_app -- new tables inherit nothing

Revision ID: f8a9b0c1d2e3
Revises: 26d72474e40d
Create Date: 2026-07-20

Phase 2 (`c4d5e6f7a8b9`) issued `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT
SELECT, INSERT, UPDATE, DELETE ON TABLES TO pwnotify_app` (and USAGE, SELECT on
sequences). No upgrade path ever took that back, so every table created afterwards
automatically inherited full CRUD for the restricted `pwnotify_app` role -- with no
RLS policy behind it. Tenant isolation therefore held only by manual enumeration:
the leak had to be cleaned up by hand for `admin_tenant` (`cd755854e58c`) and for
four provider tables (`306566b09ed2`), and the next `create_table` migration would
reopen it.

This migration removes that inheritance for good. After it, a newly created table
grants NO privileges to `pwnotify_app`. Migration authors must from now on grant
privileges EXPLICITLY, per table:

  * tenant-scoped data table  -> GRANT SELECT, INSERT, UPDATE, DELETE
                                 + ENABLE ROW LEVEL SECURITY + tenant_isolation policy
  * owner-only table (auth,   -> grant nothing (default is now no access)
    sessions, assignments,
    migration bookkeeping)

`backend/tests/test_rls_policies.py::test_app_role_grants_match_expected_soll`
enforces this: any table missing from the allow-list fails the suite.

`ALTER DEFAULT PRIVILEGES` without `FOR ROLE` targets the current role. The Phase-2
grant ran as the owner (POSTGRES_USER); this migration also runs as the owner, so the
default-ACL entries match and are removed. Existing tables keep the privileges they
were already granted -- this only changes what FUTURE tables inherit, which is intended.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: str | None = "26d72474e40d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"


def upgrade() -> None:
    # Symmetric to the downgrade of the Phase-2 migration `c4d5e6f7a8b9`: stop future tables
    # and sequences from inheriting any privilege for the restricted role.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )


def downgrade() -> None:
    # Restore the Phase-2 blanket default privileges (reconstructs the pre-migration state).
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )
