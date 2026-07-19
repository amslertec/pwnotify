"""add pwnotify_runtime login role -- non-superuser tenant-data connection

Revision ID: c2d3e4f5a6b7
Revises: 306566b09ed2
Create Date: 2026-07-19
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "306566b09ed2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The app currently connects to Postgres as the initdb superuser/table owner
# (`database_url` -> `POSTGRES_USER`). Inside a tenant-scoped transaction the code does
# `SET LOCAL ROLE pwnotify_app`, but on a superuser connection `RESET ROLE` (e.g. via a SQL
# injection in a tenant route) returns to the superuser, which bypasses RLS by table
# ownership. This migration provisions a dedicated, non-superuser LOGIN role,
# `pwnotify_runtime`, that the app uses for a second engine dedicated to tenant-scoped
# sessions only (see `app/db/session.py::get_runtime_engine`). It is a member of
# `pwnotify_app` (so `SET LOCAL ROLE pwnotify_app` still succeeds) but is itself
# NOSUPERUSER/NOBYPASSRLS -- so even a bare `RESET ROLE` on that connection lands back on a
# role that RLS still applies to. It needs no direct table grants: every tenant-scoped
# transaction always switches into `pwnotify_app` before touching a table.
ROLE = "pwnotify_runtime"
APP_ROLE = "pwnotify_app"


def upgrade() -> None:
    pw = os.environ.get("PWNOTIFY_RUNTIME_DB_PASSWORD")
    if not pw:
        raise RuntimeError(
            "PWNOTIFY_RUNTIME_DB_PASSWORD must be set to provision the pwnotify_runtime login role"
        )
    bind = op.get_bind()
    # set_config is a normal function -> accepts a bound parameter (SET/SET LOCAL do not).
    # is_local=true scopes it to this migration's transaction (alembic online = one transaction).
    bind.execute(text("SELECT set_config('pwnotify.runtime_pw', :pw, true)"), {"pw": pw})
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') THEN
            EXECUTE format(
              'CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
              current_setting('pwnotify.runtime_pw'));
          ELSE
            EXECUTE format(
              'ALTER ROLE {ROLE} WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
              current_setting('pwnotify.runtime_pw'));
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT {APP_ROLE} TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE {APP_ROLE} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    # Deliberately do NOT `DROP ROLE` here (unlike other downgrades in this chain, e.g.
    # `c4d5e6f7a8b9`'s `pwnotify_app`): `pwnotify_runtime` is a LOGIN role, so the app's
    # runtime engine holds a POOL of live physical connections authenticated as it. A
    # downgrade/upgrade round-trip against a running cluster (e.g. the migration
    # round-trip tests in this suite) would DROP and immediately re-CREATE a same-named
    # role -- Postgres treats that as a different role identity under the hood, and a
    # pooled connection that logged in before the drop then fails `SET LOCAL ROLE
    # pwnotify_app` with "permission denied to set role" afterwards, even though the grant
    # was re-added (empirically confirmed while implementing this migration). Revoking the
    # membership/schema grant already undoes everything upgrade() granted; leaving the
    # login role itself in place (now privilege-less) is safe and avoids that footgun.
