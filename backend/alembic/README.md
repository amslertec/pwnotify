# Alembic migrations

## Grant rule for `pwnotify_app` (read before adding a table)

New tables **no longer inherit any privileges** for the restricted runtime role
`pwnotify_app`. As of migration `f8a9b0c1d2e3` the schema-level `ALTER DEFAULT
PRIVILEGES ... GRANT ... TO pwnotify_app` has been revoked, so a freshly created
table grants nothing to the role by default.

When a migration creates a table, decide explicitly:

- **Tenant-scoped data table** (carries `tenant_id`): grant CRUD and isolate it.

  ```python
  op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON <tbl> TO pwnotify_app")
  op.execute("ALTER TABLE <tbl> ENABLE ROW LEVEL SECURITY")
  op.execute(
      "CREATE POLICY tenant_isolation ON <tbl> "
      "USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::int)"
  )
  ```

- **Owner-only table** (auth, sessions, tokens, assignment/group config, migration
  bookkeeping): grant **nothing**. All access runs on the owner session.

The grant model is enforced, not audited by hand:
`backend/tests/test_rls_policies.py::test_app_role_grants_match_expected_soll`
compares the effective `pwnotify_app` privileges of every base table against the
`GRANT_SOLL` allow-list. Any table missing from that dict — or with drifting
privileges — fails the suite. After adding a table, add it to `GRANT_SOLL` with the
privileges you granted (or an empty set for owner-only tables).
`test_new_table_inherits_no_app_role_privileges` additionally proves that a brand-new
table inherits no privileges, guarding against a reintroduced default-privilege grant.
