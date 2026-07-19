"""revoke pwnotify_app grants on instance-wide provider tables

Revision ID: 306566b09ed2
Revises: 798b22bf8252
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "306566b09ed2"
down_revision: str | None = "798b22bf8252"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"

# Four instance-wide provider/admin tables created after `c4d5e6f7a8b9` and therefore
# still covered by that migration's `ALTER DEFAULT PRIVILEGES ... GRANT ... TO pwnotify_app`
# blanket grant. None of them has a tenant-scoped reader (verified by grep over
# app/repositories, app/services, app/api/routes: every caller runs on the owner session --
# group console = superadmin, SSO reconcile = owner-context, token flows = owner-context
# routes -- never inside `tenant_scoped_session`/`TenantSessionDep`). Three of the four have
# no `tenant_id` column at all, so an RLS `tenant_isolation` policy is impossible for them;
# the fourth (`assignment_group_tenant`) has one but no tenant-scoped reader exists, so a
# policy would add nothing. Least privilege, matching `f7a8b9c0d1e2`: revoke all CRUD from
# `pwnotify_app` so a compromised tenant-scoped path (e.g. SQL injection in a tenant route)
# cannot read `user_token.token_hash` (invite/reset token secrets), read or tamper with
# `assignment_group`/`assignment_group_tenant` (Entra-group-to-tenant mapping config), or
# read `assignment_group_member` (Entra member PII: UPN, display name, mail).
PROVIDER_TABLES = (
    "user_token",
    "assignment_group",
    "assignment_group_tenant",
    "assignment_group_member",
)


def upgrade() -> None:
    for tbl in PROVIDER_TABLES:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {tbl} FROM {APP_ROLE}")


def downgrade() -> None:
    # Restore the Phase-2 blanket state for these four tables (table-specific, not via
    # `ALL TABLES IN SCHEMA public`, so the rest of the role's grants stay untouched).
    for tbl in PROVIDER_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO {APP_ROLE}")
