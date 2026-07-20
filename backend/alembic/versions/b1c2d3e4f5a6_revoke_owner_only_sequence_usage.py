"""revoke USAGE on owner-only / read-only sequences from pwnotify_app (F-04)

Revision ID: b1c2d3e4f5a6
Revises: a9c0d1e2f3a4
Create Date: 2026-07-20

The S1 migration (`f8a9b0c1d2e3`) took back the *default* privileges so future tables
and sequences inherit nothing, but it deliberately left EXISTING grants untouched (the
running app still writes tenant data). Side effect: the Phase-2 blanket
`GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pwnotify_app` still leaves
`pwnotify_app` with USAGE on the id sequences of tables it can no longer touch.

This makes the sequence-level grants consistent with the table-level grants: the runtime
role may advance a table's id sequence exactly when it may INSERT into that table. Those
INSERTs run only on the tenant-scoped RLS tables (entra_user, exclusion, notification_log,
run, audit_log -- `setting` has no serial sequence). Every other table is either owner-only
(app_user, user_token, user_session, assignment_group, assignment_group_member) or
SELECT-only (`tenant`); its rows are created exclusively on the owner/superuser session,
which bypasses grants, so `pwnotify_app` never needs their sequences. Practical impact of
the leftover USAGE was near zero (a compromised tenant path could only burn id numbers, no
data access), but the grant model must hold by construction, not by luck.

`REVOKE ALL` drops the leftover USAGE and SELECT for these six sequences. The downgrade
restores the exact pre-migration state (USAGE + SELECT, as handed out by the Phase-2
blanket grant).

`backend/tests/test_rls_policies.py::test_app_role_sequence_usage_matches_table_grants`
enforces the invariant: any sequence whose USAGE no longer mirrors its table's INSERT
grant -- or whose owning table is absent from the grant allow-list -- turns the suite red.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "pwnotify_app"

# Sequences of tables `pwnotify_app` cannot INSERT into (owner-only + SELECT-only `tenant`).
# Their ids are only ever advanced on the owner session, which bypasses grants.
_OWNER_ONLY_SEQUENCES = (
    "app_user_id_seq",
    "user_token_id_seq",
    "user_session_id_seq",
    "assignment_group_id_seq",
    "assignment_group_member_id_seq",
    "tenant_id_seq",
)


def upgrade() -> None:
    for seq in _OWNER_ONLY_SEQUENCES:
        op.execute(f"REVOKE ALL ON SEQUENCE {seq} FROM {APP_ROLE}")


def downgrade() -> None:
    # Reconstruct the pre-migration state: these six carried USAGE + SELECT from the Phase-2
    # blanket `GRANT USAGE, SELECT ON ALL SEQUENCES` (`c4d5e6f7a8b9`).
    for seq in _OWNER_ONLY_SEQUENCES:
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {APP_ROLE}")
