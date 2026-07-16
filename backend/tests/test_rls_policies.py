import pytest
from app.db.rls import APP_ROLE, RLS_TABLES
from sqlalchemy import text


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
