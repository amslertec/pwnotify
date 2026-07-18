"""SECURITY ACCEPTANCE GATE (Group-Roles Task 7): the end-to-end adversarial matrix that
proves team-driven SSO login produces the correct HOME role AND the correct per-customer
GRANTS, that the provider-only tenant-isolation invariant holds, and that the customer /
single-tenant paths are byte-for-byte unaffected. A red cell here is a real hole and blocks
the branch -- it sends work back to Task 3 (reconcile) or Task 4 (login authorization).

Everything is driven through the REAL `auth.oidc_callback` (the callback fake, `_result` and
`_write_mode` are REUSED verbatim from `test_oidc_group_auth.py` so the matrix cannot drift
from the Task-4 auth suite) and asserted DIRECTLY on the `admin_tenant`/`auditor_tenant`
grant tables and via RLS-scoped sessions against REAL Postgres (RLS participates).

Because the callback opens its OWN second connections (`instance_settings.read_mode`,
`tenant_scoped_session`, `get_session_factory`) and the grant reconcile commits internally,
ALL seed data must be committed -- the fixture seeds via a dedicated committed connection on
`migrated_engine` and tears everything down in a `finally` (same seam as
`test_oidc_group_auth.py` / `test_group_sync_matrix_adversarial.py`). The shared default
tenant is reset exactly; every test sets the multi-tenant mode itself so no test depends on
ordering.

Topology (multi-tenant): default (provider home) + customers A, B, C.
  T1 = `_G1` role=admin   -> {A, C}
  T2 = `_G2` role=auditor -> {B, C}
Disjoint settings role-groups (never a Team id): `_DEFAULT_SETTINGS_ADMIN` on the default
tenant (single-tenant path), `_A_SETTINGS_ADMIN` on customer A (customer path).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.models.audit import AuditLog
from app.models.tenant import AdminTenant, AuditorTenant
from app.models.user import AppUser
from app.repositories import assignment_group_repo, tenant_repo, user_repo
from app.services.audit import LOGIN_FAILED
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

# REUSE the Task-4 callback fake + result/mode helpers verbatim -- one source of truth so the
# matrix and the auth suite cannot diverge (brief: "REUSE its harness/seeding verbatim").
from tests.test_oidc_group_auth import _call_oidc_callback, _result, _write_mode

_DOMAIN = "@grpmatrix.test"

_TID_DEFAULT = "grpmatrix-default-tid"
_TID_A = "grpmatrix-a-tid"

# Provider-Teams (instanzweit). Every dynamic team an individual test creates ALSO carries the
# `grpmatrix-` prefix so the fixture's prefix-scoped cleanup catches it.
_PREFIX = "grpmatrix-"
_G1 = "grpmatrix-team-admin-ac"  # role=admin   -> {A, C}
_G2 = "grpmatrix-team-auditor-bc"  # role=auditor -> {B, C}

# Settings role-groups (per tenant) -- deliberately DISJOINT from every Team id.
_DEFAULT_SETTINGS_ADMIN = "grpmatrix-default-settings-admin"
_A_SETTINGS_ADMIN = "grpmatrix-a-settings-admin"


class _Env:
    default_id: int
    a_id: int
    b_id: int
    c_id: int


# --------------------------------------------------------------------------------------- #
# Grant-table assertion helpers -- (tenant_id, source) pairs read DIRECTLY off each table.
# --------------------------------------------------------------------------------------- #


async def _admin_pairs(session: AsyncSession, user_id: int) -> set[tuple[int, str]]:
    rows = (
        await session.execute(
            select(AdminTenant.tenant_id, AdminTenant.source).where(AdminTenant.user_id == user_id)
        )
    ).all()
    return {(int(t), str(s)) for t, s in rows}


async def _auditor_pairs(session: AsyncSession, user_id: int) -> set[tuple[int, str]]:
    rows = (
        await session.execute(
            select(AuditorTenant.tenant_id, AuditorTenant.source).where(
                AuditorTenant.user_id == user_id
            )
        )
    ).all()
    return {(int(t), str(s)) for t, s in rows}


@pytest_asyncio.fixture
async def env(migrated_engine: AsyncEngine) -> AsyncGenerator[_Env]:
    async with migrated_engine.connect() as conn:
        default_id = (
            await conn.execute(text("SELECT id FROM tenant WHERE is_default"))
        ).scalar_one()
        orig_entra = (
            await conn.execute(
                text("SELECT entra_tenant_id FROM tenant WHERE id = :id"), {"id": default_id}
            )
        ).scalar_one()

        # Bind the default tenant to a known tid so a provider login matches it.
        await conn.execute(
            text("UPDATE tenant SET entra_tenant_id = :tid WHERE id = :id"),
            {"tid": _TID_DEFAULT, "id": default_id},
        )
        # Default settings-admin group (only the single-tenant path consumes it).
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:id, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now()) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"id": default_id, "g": _DEFAULT_SETTINGS_ADMIN},
        )
        # Customers A, B, C -- A carries its own tid + settings-admin group (customer path).
        a_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('GrpMtxA','grpmatrix-a',:tid,true,now()) RETURNING id"
                ),
                {"tid": _TID_A},
            )
        ).scalar_one()
        b_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('GrpMtxB','grpmatrix-b',true,now()) RETURNING id"
                )
            )
        ).scalar_one()
        c_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('GrpMtxC','grpmatrix-c',true,now()) RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO setting (tenant_id, key, value, is_secret, updated_at) "
                "VALUES (:id, 'oidc.admin_group_id', to_jsonb(CAST(:g AS text)), false, now())"
            ),
            {"id": a_id, "g": _A_SETTINGS_ADMIN},
        )
        # Teams: T1 admin -> {A, C}, T2 auditor -> {B, C}.
        t1_id = (
            await conn.execute(
                text(
                    "INSERT INTO assignment_group (name, entra_group_id, role, created_at) "
                    "VALUES ('T1-Admin', :g, 'admin', now()) RETURNING id"
                ),
                {"g": _G1},
            )
        ).scalar_one()
        t2_id = (
            await conn.execute(
                text(
                    "INSERT INTO assignment_group (name, entra_group_id, role, created_at) "
                    "VALUES ('T2-Auditor', :g, 'auditor', now()) RETURNING id"
                ),
                {"g": _G2},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO assignment_group_tenant (assignment_group_id, tenant_id) VALUES "
                "(:t1,:a),(:t1,:c),(:t2,:b),(:t2,:c)"
            ),
            {"t1": t1_id, "t2": t2_id, "a": a_id, "b": b_id, "c": c_id},
        )
        await conn.commit()

        fx = _Env()
        fx.default_id, fx.a_id, fx.b_id, fx.c_id = default_id, a_id, b_id, c_id
        try:
            yield fx
        finally:
            await conn.execute(
                text(
                    "DELETE FROM user_session WHERE user_id IN "
                    f"(SELECT id FROM app_user WHERE username LIKE '%{_DOMAIN}')"
                )
            )
            await conn.execute(
                text(f"DELETE FROM audit_log WHERE actor_username LIKE '%{_DOMAIN}'")
            )
            # Dropping the users cascades their admin_tenant/auditor_tenant grant rows (FK CASCADE).
            await conn.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_DOMAIN}'"))
            # Prefix-scoped: catches T1/T2 AND any team a test created; cascades group_tenant.
            await conn.execute(
                text("DELETE FROM assignment_group WHERE entra_group_id LIKE :p"),
                {"p": f"{_PREFIX}%"},
            )
            await conn.execute(
                text(
                    "DELETE FROM setting WHERE tenant_id = :id AND key IN "
                    "('oidc.admin_group_id', 'instance.multi_tenant_mode')"
                ),
                {"id": default_id},
            )
            await conn.execute(
                text("UPDATE tenant SET entra_tenant_id = :orig WHERE id = :id"),
                {"orig": orig_entra, "id": default_id},
            )
            # Dropping customers cascades their run rows + any residual grants (FK CASCADE).
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b, :c)"),
                {"a": a_id, "b": b_id, "c": c_id},
            )
            await conn.commit()


# ========================================================================================= #
# CELL 1 -- Team role -> home role + customer grants (happy path, admin-wins).
#   Provider login (tid=default, groups=[g1,g2]) -> user.role == 'admin' (home; admin-wins
#   over the auditor team) AND grants EXACTLY admin_tenant(A,group), admin_tenant(C,group)
#   (C admin-wins), auditor_tenant(B,group); ZERO auditor(A/C) / admin(B). Asserts on both
#   grant tables directly.
# ========================================================================================= #


async def test_cell1_team_role_to_home_role_and_grants_admin_wins(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell1-provider{_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, [_G1, _G2]))
    assert isinstance(resp, RedirectResponse)
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None and user.id is not None
        assert user.tenant_id == env.default_id
        assert user.role == "admin"  # home role = admin-wins across the two teams

        # EXACT per-customer grants: admin{A,C} (C admin-wins over T2's auditor mapping),
        # auditor{B}. Any cross-kind bleed (auditor A/C or admin B) turns this cell RED.
        assert await _admin_pairs(session, user.id) == {
            (env.a_id, "group"),
            (env.c_id, "group"),
        }
        assert await _auditor_pairs(session, user.id) == {(env.b_id, "group")}


# ========================================================================================= #
# CELL 2 -- Provider-only isolation invariant. A customer-A-homed SSO admin whose token groups
#   ALSO match provider Teams resolves via settings (customer path) and reconcile is a no-op;
#   a NULL-home account with provider-Team groups is likewise a no-op. ZERO provider-Team grant
#   rows for EITHER -- asserted directly on both tables. The `is_provider_account` gate (first
#   line of reconcile) is the only thing that stops a foreign grant -- see the non-vacuity note.
# ========================================================================================= #


async def test_cell2_customer_and_null_home_get_zero_provider_grants(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)

    # (a) Customer-A-homed admin via the REAL login: groups carry BOTH provider Teams and A's
    #     settings-admin group. The customer path (tenant A != default) authorizes via settings
    #     -> allowed; reconcile then no-ops because the home tenant is A, not default.
    cust_user = f"cell2-customer-a{_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(cust_user, _TID_A, [_G1, _G2, _A_SETTINGS_ADMIN])
        )
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        cust = await user_repo.get_by_username(session, cust_user)
        assert cust is not None and cust.id is not None
        assert cust.tenant_id == env.a_id  # homed at the CUSTOMER, not the default tenant
        # No provider-Team grant materialized for a customer-homed account, either table.
        assert await _admin_pairs(session, cust.id) == set()
        assert await _auditor_pairs(session, cust.id) == set()

    # (b) NULL-home account driven straight through the reconcile entry point (a login can never
    #     yield tenant_id IS NULL -- it always resolves or denies -- so the gate is exercised by
    #     calling the same function the login calls). The gate must short-circuit to a no-op.
    async with get_session_factory()() as session:
        nulluser = AppUser(
            username=f"cell2-null-home{_DOMAIN}",
            password_hash="x",
            role="admin",
            is_sso=True,
            tenant_id=None,
        )
        session.add(nulluser)
        await session.flush()
        assert nulluser.id is not None
        await assignment_group_repo.reconcile_group_grants(session, nulluser, [_G1, _G2])
        assert await _admin_pairs(session, nulluser.id) == set()
        assert await _auditor_pairs(session, nulluser.id) == set()
        # Never committed -> the closing rollback drops the throwaway NULL-home row.


# ========================================================================================= #
# CELL 3 -- Login-deny (no team). Multi-tenant ON, tid=default, groups match NO Team -> login
#   denied (sso_denied=1, LOGIN_FAILED / not_in_any_team) and NO user row is created and NO
#   grant row exists for the username.
# ========================================================================================= #


async def test_cell3_no_team_login_denied_no_side_effects(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell3-no-team{_DOMAIN}"

    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(username, _TID_DEFAULT, ["grpmatrix-not-a-team"])
        )
    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, username) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.outcome == "failure"
        assert row.detail.get("reason") == "not_in_any_team"
        # Fail-closed: no user was created, so no grant row can reference this username, in
        # either table (join through app_user to be explicit about "no grant for this login").
        for model in (AdminTenant, AuditorTenant):
            leaked = (
                await session.execute(
                    select(model)
                    .join(AppUser, AppUser.id == model.user_id)
                    .where(AppUser.username == username)
                )
            ).first()
            assert leaked is None


# ========================================================================================= #
# CELL 4 -- Customer path unaffected. tid=customer A -> settings role-groups decide; a provider-
#   Team member who is NOT in A's settings-admin group is DENIED (Team membership is irrelevant
#   on the customer path) and gets ZERO grants.
# ========================================================================================= #


async def test_cell4_customer_path_ignores_team_membership(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell4-team-but-not-a-settings{_DOMAIN}"

    # In a provider ADMIN team, but NOT in customer A's settings-admin group -> DENIED.
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_A, [_G1]))
    assert resp.status_code == 302
    assert "sso_denied=1" in resp.headers["location"]

    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, username) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        # The denial reason comes from resolve_role (settings path), NOT the Team path.
        assert row.detail.get("reason") != "not_in_any_team"


# ========================================================================================= #
# CELL 5 -- Single-tenant path unaffected. read_mode OFF, tid=default -> settings decide. A
#   Team-only member is DENIED; a settings-admin member is allowed, role=admin, and gets NO
#   group grants (this member is in no Team -> reconcile's desired sets are empty).
# ========================================================================================= #


async def test_cell5_single_tenant_path_uses_settings_no_group_grants(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, False)  # multi-tenant OFF

    # (a) Team-only member (not in the default settings-admin group) -> DENIED.
    denied = f"cell5-team-only{_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(denied, _TID_DEFAULT, [_G1]))
    assert "sso_denied=1" in resp.headers["location"]
    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, denied) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == denied))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        assert row.detail.get("reason") != "not_in_any_team"

    # (b) Settings-admin member -> allowed (role=admin) and ZERO group grants.
    ok = f"cell5-settings-admin{_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(
            session, _result(ok, _TID_DEFAULT, [_DEFAULT_SETTINGS_ADMIN])
        )
    assert "sso_denied" not in resp.headers["location"]
    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, ok)
        assert user is not None and user.id is not None
        assert user.tenant_id == env.default_id
        assert user.role == "admin"
        # A settings-admin who is in no Team -> empty desired sets -> no group grants at all.
        assert await _admin_pairs(session, user.id) == set()
        assert await _auditor_pairs(session, user.id) == set()


# ========================================================================================= #
# CELL 6 -- Role flip across logins. Provider P logs in with an ADMIN team mapping C ->
#   admin_tenant(C,group); the team is re-roled to auditor; P logs in again -> the per-table
#   reconcile clears the stale admin(C) row and materializes auditor(C,group). ZERO admin(C).
#   A dedicated team maps ONLY C so the flip assertion is crisp.
# ========================================================================================= #


async def test_cell6_role_flip_across_logins(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    gc = "grpmatrix-flip-c"  # dedicated team -> {C}; cleaned by the prefix-scoped teardown.
    username = f"cell6-provider{_DOMAIN}"

    async with migrated_engine.connect() as conn:
        gc_id = (
            await conn.execute(
                text(
                    "INSERT INTO assignment_group (name, entra_group_id, role, created_at) "
                    "VALUES ('FlipC', :g, 'admin', now()) RETURNING id"
                ),
                {"g": gc},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO assignment_group_tenant (assignment_group_id, tenant_id) "
                "VALUES (:g, :c)"
            ),
            {"g": gc_id, "c": env.c_id},
        )
        await conn.commit()

    # Login 1: admin team -> admin_tenant(C, group).
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, [gc]))
    assert "sso_denied" not in resp.headers["location"]
    async with get_session_factory()() as session:
        user = await user_repo.get_by_username(session, username)
        assert user is not None and user.id is not None
        uid = user.id
        assert await _admin_pairs(session, uid) == {(env.c_id, "group")}
        assert await _auditor_pairs(session, uid) == set()

    # Superadmin re-roles the team admin -> auditor (committed so the next login sees it).
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text("UPDATE assignment_group SET role = 'auditor' WHERE entra_group_id = :g"),
            {"g": gc},
        )
        await conn.commit()

    # Login 2 (same membership): per-table reconcile flips the grant kind and clears the stale
    # admin(C) row.
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, [gc]))
    assert "sso_denied" not in resp.headers["location"]
    async with get_session_factory()() as session:
        assert await _auditor_pairs(session, uid) == {(env.c_id, "group")}
        assert await _admin_pairs(session, uid) == set()  # ZERO stale admin(C)


# ========================================================================================= #
# CELL 7 -- RLS backstop end-to-end. After P holds admin_tenant(A,group):
#   control plane -- is_allowed(P,A,write) True; a forged active-tenant for B (no B grant) is
#     denied (is_allowed False, read AND write);
#   data plane (RLS, REAL committed rows) -- a tenant-scoped session for A returns A's data and
#     ONLY A's; scoping to B never leaks A's rows;
#   then P re-logs in with EMPTY groups (left every team) -> login DENIED (not_in_any_team),
#   so P can no longer authenticate into A at all.
# ========================================================================================= #


async def test_cell7_rls_backstop_end_to_end(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell7-provider{_DOMAIN}"

    # Login with the admin team T1 -> admin_tenant(A, group) (and C).
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, [_G1]))
    assert "sso_denied" not in resp.headers["location"]

    # Seed REAL committed run rows on A and B for the RLS data-plane probe.
    async with migrated_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO run (tenant_id, trigger, dry_run, status, started_at, "
                "checked_users, sent, failed, skipped, detail_log) VALUES "
                "(:a,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb), "
                "(:b,'manual',false,'ok',now(),0,0,0,0,'[]'::jsonb)"
            ),
            {"a": env.a_id, "b": env.b_id},
        )
        await conn.commit()
    try:
        async with get_session_factory()() as session:
            user = await user_repo.get_by_username(session, username)
            assert user is not None and user.id is not None
            assert await _admin_pairs(session, user.id) == {
                (env.a_id, "group"),
                (env.c_id, "group"),
            }
            # Control-plane gate: A allowed (write); forged B denied (no B grant), read + write.
            assert await tenant_repo.is_allowed(session, user, env.a_id, write=True) is True
            assert await tenant_repo.is_allowed(session, user, env.b_id, write=True) is False
            assert await tenant_repo.is_allowed(session, user, env.b_id, write=False) is False

        # Data-plane RLS backstop on the committed run rows.
        async with tenant_scoped_session(env.a_id) as s:
            rows_a = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
        assert set(rows_a) == {env.a_id}, f"RLS leak while scoped to A: {rows_a}"
        async with tenant_scoped_session(env.b_id) as s:
            rows_b = (await s.execute(text("SELECT tenant_id FROM run"))).scalars().all()
        assert set(rows_b) == {env.b_id}, f"RLS leak while scoped to B: {rows_b}"
        assert env.a_id not in set(rows_b), "Cross-tenant leak: A visible under B's scope"
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM run WHERE tenant_id IN (:a, :b)"),
                {"a": env.a_id, "b": env.b_id},
            )
            await conn.commit()

    # P leaves every team: an empty-groups re-login is DENIED (not_in_any_team) -- P can no
    # longer authenticate into A.
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, []))
    assert "sso_denied=1" in resp.headers["location"]
    async with get_session_factory()() as session:
        denied = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.actor_username == username, AuditLog.action == LOGIN_FAILED)
                .order_by(AuditLog.id.desc())
            )
        ).first()
        assert denied is not None
        assert denied[0].detail.get("reason") == "not_in_any_team"


# ========================================================================================= #
# CELL 8 -- Manual precedence survives login. A manual admin_tenant(A, manual) given first stays
#   source='manual' through a group-based login that ALSO maps A (via T1 admin -> {A,C}); C is
#   materialized as source='group'. No conversion, no duplicate row on A.
# ========================================================================================= #


async def test_cell8_manual_grant_survives_group_login(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell8-provider{_DOMAIN}"

    # Pre-seed provider P (homed at default) via the ORM (model defaults) + a MANUAL admin grant
    # on A, all committed so the login's own session sees them.
    async with get_session_factory()() as session:
        p = AppUser(
            username=username,
            password_hash="x",
            role="admin",
            is_sso=True,
            tenant_id=env.default_id,
        )
        session.add(p)
        await session.commit()
        assert p.id is not None
        uid = p.id
        await tenant_repo.add_grant(
            session, user_id=uid, tenant_id=env.a_id, kind="admin", source="manual"
        )

    # Group-based login: T1 (admin) maps A AND C.
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(username, _TID_DEFAULT, [_G1]))
    assert "sso_denied" not in resp.headers["location"]

    async with get_session_factory()() as session:
        # A stays EXACTLY one row, still source='manual'; C added as source='group'. A bug that
        # converted the manual row or added a duplicate turns this cell RED.
        assert await _admin_pairs(session, uid) == {
            (env.a_id, "manual"),
            (env.c_id, "group"),
        }
        assert await _auditor_pairs(session, uid) == set()
