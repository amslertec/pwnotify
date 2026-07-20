import pytest
from app.db.rls import APP_ROLE, RLS_TABLES
from sqlalchemy import text

# Expected effective privileges of the restricted `pwnotify_app` role, per table. This is the
# authoritative allow-list: the grant model must hold by construction, not by manual audit.
# Any base table in schema `public` that is NOT listed here fails the soll test below, forcing
# the author of a new migration to make a deliberate decision (grant + RLS for tenant data, or
# nothing for owner-only tables). See `backend/alembic/README.md`.
GRANT_SOLL: dict[str, frozenset[str]] = {
    # SELECT-only: instance-wide tables a future tenant switcher may need to read. They hold
    # no secrets; INSERT/UPDATE/DELETE run exclusively on the owner session.
    "tenant": frozenset({"SELECT"}),
    "admin_tenant": frozenset({"SELECT"}),
    "auditor_tenant": frozenset({"SELECT"}),
    # CRUD + RLS policy: tenant-scoped data tables, isolated by the `tenant_isolation` policy.
    "entra_user": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "exclusion": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "notification_log": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "run": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "setting": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "audit_log": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    # No privileges: auth/session/token secrets, Entra group-mapping config + member PII, and
    # migration bookkeeping. Every reader runs on the owner session; `pwnotify_app` never touches
    # these, so a compromised tenant-scoped path cannot reach them.
    "user_token": frozenset(),
    "app_user": frozenset(),
    "user_session": frozenset(),
    "assignment_group": frozenset(),
    "assignment_group_tenant": frozenset(),
    "assignment_group_member": frozenset(),
    "alembic_version": frozenset(),
}

_CRUD = ("SELECT", "INSERT", "UPDATE", "DELETE")


async def _effective_privs(session, table: str) -> set[str]:
    """Effective CRUD privileges of `pwnotify_app` on `table` (robust against inheritance)."""
    granted: set[str] = set()
    for priv in _CRUD:
        ok = (
            await session.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": APP_ROLE, "t": f"public.{table}", "p": priv},
            )
        ).scalar_one()
        if ok:
            granted.add(priv)
    return granted


async def test_app_role_grants_match_expected_soll(session):
    """Every base table's `pwnotify_app` grants must match `GRANT_SOLL` exactly, and every
    table must be listed. Guards the whole default-privilege failure class: a new table that
    silently inherited grants (or was forgotten in the allow-list) turns this test red."""
    tables = (
        (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            )
        )
        .scalars()
        .all()
    )
    for tbl in tables:
        assert tbl in GRANT_SOLL, (
            f"table {tbl!r} has no defined grant expectation -- after adding a new migration, "
            f"record it in GRANT_SOLL and grant privileges to {APP_ROLE} explicitly "
            "(GRANT + ENABLE RLS + policy for tenant data; nothing for owner-only tables)"
        )
        effective = await _effective_privs(session, tbl)
        assert effective == set(GRANT_SOLL[tbl]), (
            f"grant drift on {tbl}: expected {sorted(GRANT_SOLL[tbl])}, got {sorted(effective)}"
        )


async def test_new_table_inherits_no_app_role_privileges(session):
    """S1 behavioural proof: a freshly created table must NOT inherit any `pwnotify_app`
    privileges. Before the default-privileges revoke migration this was red -- `ALTER DEFAULT
    PRIVILEGES ... GRANT` handed every new table full CRUD to `pwnotify_app` without an RLS
    policy, so tenant isolation held only by manual per-table cleanup."""
    async with session.begin_nested() as savepoint:
        await session.execute(text("CREATE TABLE _grant_probe (id int)"))
        effective = await _effective_privs(session, "_grant_probe")
        await savepoint.rollback()  # never persist the probe table
    assert effective == set(), (
        f"new table _grant_probe inherited {sorted(effective)} for {APP_ROLE} -- default "
        "privileges still leak grants to newly created tables"
    )


async def test_app_role_exists_and_is_restricted(session):
    row = (
        await session.execute(
            text("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": APP_ROLE},
        )
    ).one_or_none()
    assert row is not None, "pwnotify_app-Rolle fehlt"
    assert row.rolsuper is False and row.rolbypassrls is False and row.rolcanlogin is False


async def test_rls_enabled_on_all_tenant_tables(session):
    for tbl in RLS_TABLES:
        enabled = (
            await session.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": tbl}
            )
        ).scalar_one()
        assert enabled is True, f"RLS nicht aktiv auf {tbl}"


async def test_isolation_enforced_under_app_role(session):
    # Zwei Tenants + je eine setting-Zeile anlegen (als Superuser, RLS umgangen).
    await session.execute(
        text(
            "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
            "('T-A','iso-a',true,now()), ('T-B','iso-b',true,now())"
        )
    )
    ids = (
        (
            await session.execute(
                text("SELECT id FROM tenant WHERE slug IN ('iso-a','iso-b') ORDER BY slug")
            )
        )
        .scalars()
        .all()
    )
    a, b = ids[0], ids[1]
    await session.execute(
        text(
            "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) VALUES "
            "(:a,'k','\"va\"'::jsonb,false,now()), (:b,'k','\"vb\"'::jsonb,false,now())"
        ),
        {"a": a, "b": b},
    )
    await session.flush()

    # In die App-Rolle wechseln + Tenant A setzen → nur A-Zeile sichtbar.
    await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await session.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a)})
    seen = (
        (await session.execute(text("SELECT tenant_id FROM setting WHERE key='k'"))).scalars().all()
    )
    assert seen == [a], f"RLS-Leak: erwartet nur {a}, sah {seen}"

    # Cross-Tenant-Write muss scheitern.
    with pytest.raises(Exception) as exc:
        await session.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:b,'x','\"y\"'::jsonb,false,now())"
            ),
            {"b": b},
        )
        await session.flush()
    assert "row-level security" in str(exc.value).lower()
    await session.rollback()
