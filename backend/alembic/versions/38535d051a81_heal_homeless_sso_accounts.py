"""heal homeless sso accounts

Revision ID: 38535d051a81
Revises: b1c2d3e4f5a6
Create Date: 2026-07-20 14:34:36.144025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "38535d051a81"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Data healing, no schema change: prod logs show pre-existing SSO accounts (is_sso=true)
    # with tenant_id IS NULL -- legacy rows from before a home tenant was assigned on every
    # SSO path (OIDC callback + sync_sso_users both set tenant_id today). A homeless SSO
    # account is not merely inert: the M8 foreign-tenant guard in `sync_sso_users`
    # (app/services/oidc.py) skips ANY account whose `tenant_id != sync_tenant` -- and
    # `NULL != sync_tenant` is true for every tenant that ever runs a sync. The account is
    # therefore skipped on every single sync, forever, logged as
    # `sso_sync_foreign_tenant_conflict`, and never self-heals.
    #
    # Product-owner decision: heal all such accounts onto the provider default tenant. In
    # multi-tenant mode every SSO account is homed somewhere; the default tenant is the only
    # sensible home for a pre-multi-tenant-era SSO account with no recorded home. Identified
    # via `is_default` (not `slug = 'default'`): the slug is renamable (see `4035552093e2`),
    # `is_default` is the stable, slug-independent incumbent marker.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE app_user SET tenant_id = (SELECT id FROM tenant WHERE is_default) "
            "WHERE is_sso = true AND tenant_id IS NULL"
        )
    )


def downgrade() -> None:
    # Intentional no-op: this is a data heal, not a schema change, and the original NULLs
    # are not reconstructible -- healed rows are indistinguishable from SSO accounts that
    # were always correctly homed on the default tenant. Reverting would silently un-home
    # accounts that other migrations/paths now depend on having a home.
    pass
