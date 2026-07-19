"""SECURITY ACCEPTANCE GATE (Tenant-Refinements Task 6): the end-to-end adversarial matrix
that proves the WHOLE tenant-refinements batch holds together against a real Postgres (RLS
participates). A red cell here is a real hole and blocks the branch -- it sends work back to
Task 1 (sync-guard), Task 3 (deprovision-delete) or Task 4 (provider-only SSO login/homing).

The three shipped invariants under test, all driven through the REAL entry points:
  * provider-only SSO (`auth.oidc_callback`): in multi-tenant mode EVERY SSO login is
    authorized group-based (`resolve_group_role`) and homed on the DEFAULT tenant, regardless
    of the matched `tid`; single-tenant is byte-for-byte unchanged (`resolve_role`);
  * deprovision-delete (`group_sync.sync_group`): a fully-deprovisioned provider SSO account
    is DELETED after the grant reconcile, but only under the full fail-safe gate;
  * sync-guard (`graph.sync.sync_users` + `runner.execute_run`): an unconfigured Graph skips
    cleanly with NO GraphClient / NO MSAL call and a benign run.

Every harness is REUSED VERBATIM from the per-task adversarial suites so the gate cannot drift
from them (brief: "REUSE ... verbatim"):
  * the SSO-callback fake + `_result`/`_write_mode` from `test_oidc_group_auth`,
  * the deprovision seeding + fake `get_group_members` + table assertions from
    `test_group_sync_deprovision_adversarial`,
  * the unconfigured-Graph run harness + the GraphClient-never-built spy from
    `test_runner_sync_guard`.

Because the callback opens its OWN second connections (`instance_settings.read_mode`,
`tenant_scoped_session`, `get_session_factory`) and the grant reconcile commits internally, all
SSO/RLS seed data is COMMITTED via a dedicated connection on `migrated_engine` and torn down in
a `finally`; the shared default tenant is reset exactly and every test sets the multi-tenant
mode itself so no test depends on ordering. The deprovision cells run under the transactional
`session` fixture (rolled back), exactly like the suite they reuse.

Topology (multi-tenant): default (provider home) + customers A, B. One provider Team:
  T1 = `_G1` role=admin -> {A}.
Disjoint settings role-groups (never a Team id): `_DEFAULT_SETTINGS_ADMIN` on the default
tenant (single-tenant path), `_A_SETTINGS_ADMIN` on customer A.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.db.session import get_session_factory
from app.db.tenant_context import open_active_session, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.tenant import AdminTenant, AuditorTenant
from app.models.user import AppUser, UserSession
from app.repositories import assignment_group_member_repo as member_repo
from app.repositories import assignment_group_repo, tenant_repo, user_repo
from app.services import group_sync
from app.services.audit import LOGIN_FAILED
from app.services.graph import sync as graph_sync
from app.services.scheduler import SchedulerService
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

# REUSE the Task-3 deprovision seeding + fake Graph + table assertions verbatim.
from tests.test_group_sync_deprovision_adversarial import (
    _admin_rows,
    _auditor_rows,
    _member,
    _mk_session,
    _mk_team,
    _mk_tenant,
    _mk_user,
    _patch_graph,
    _session_count,
    _user_exists,
)

# REUSE the Task-4 SSO callback fake + result/mode helpers verbatim.
from tests.test_oidc_group_auth import _call_oidc_callback, _result, _write_mode

# REUSE the Task-1 unconfigured-Graph run harness + the GraphClient-never-built spy verbatim.
from tests.test_runner_sync_guard import (
    _boom_if_constructed,
    _patch_everything_but_sync,
    _real_default_tenant_id,
)

_DOMAIN = "@tenref.test"

_TID_DEFAULT = "tenref-default-tid"
_TID_A = "tenref-a-tid"

# Provider teams (instance-wide). Prefix-scoped teardown catches every one.
_PREFIX = "tenref-"
_G1 = "tenref-team-admin-a"  # role=admin -> {A}

# Settings role-groups (per tenant) -- deliberately DISJOINT from every Team id.
_DEFAULT_SETTINGS_ADMIN = "tenref-default-settings-admin"
_A_SETTINGS_ADMIN = "tenref-a-settings-admin"


class _Env:
    default_id: int
    a_id: int
    b_id: int


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
        # Customer A carries its own tid + settings-admin group (customer path).
        a_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, entra_tenant_id, is_active, created_at) "
                    "VALUES ('TenRefA','tenref-a',:tid,true,now()) RETURNING id"
                ),
                {"tid": _TID_A},
            )
        ).scalar_one()
        b_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenant (name, slug, is_active, created_at) "
                    "VALUES ('TenRefB','tenref-b',true,now()) RETURNING id"
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
        # Team T1 admin -> {A}.
        t1_id = (
            await conn.execute(
                text(
                    "INSERT INTO assignment_group (name, entra_group_id, role, created_at) "
                    "VALUES ('T1-Admin', :g, 'admin', now()) RETURNING id"
                ),
                {"g": _G1},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO assignment_group_tenant (assignment_group_id, tenant_id) "
                "VALUES (:t1, :a)"
            ),
            {"t1": t1_id, "a": a_id},
        )
        await conn.commit()

        fx = _Env()
        fx.default_id, fx.a_id, fx.b_id = default_id, a_id, b_id
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
            # Dropping the users cascades their grant rows (FK CASCADE).
            await conn.execute(text(f"DELETE FROM app_user WHERE username LIKE '%{_DOMAIN}'"))
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
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"),
                {"a": a_id, "b": b_id},
            )
            await conn.commit()


# ========================================================================================= #
# CELL 1 -- MT SSO is ALWAYS group-based + default-homed, including a customer `tid`; and a
#   customer-homed SSO account gets ZERO group grants (the `is_provider_account` gate).
#     (a) tid=default, groups=[g1] -> role admin, home==default, session active_tenant==default,
#         admin_tenant(A, group) materialized.
#     (b) tid=A (customer), groups=[g1] -> IDENTICAL outcome (default-homed, admin_tenant(A)).
#     (c) a customer-A-homed SSO account (created via the ORM, since MT SSO can no longer produce
#         a customer home) driven through the reconcile with [g1] -> ZERO grants, either table.
# ========================================================================================= #


async def test_cell1_mt_sso_group_based_default_homed_and_customer_gets_zero_grants(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)

    async def _assert_provider_login(username: str, tid: str) -> None:
        async with get_session_factory()() as session:
            resp = await _call_oidc_callback(session, _result(username, tid, [_G1]))
        assert isinstance(resp, RedirectResponse)
        assert "sso_denied" not in resp.headers["location"]

        async with get_session_factory()() as session:
            user = await user_repo.get_by_username(session, username)
            assert user is not None and user.id is not None
            assert user.role == "admin"
            assert user.tenant_id == env.default_id  # default-homed, NOT the matched tenant
            sess = (
                await session.execute(select(UserSession).where(UserSession.user_id == user.id))
            ).scalar_one()
            assert sess.active_tenant_id == env.default_id
            assert user.tenant_id == sess.active_tenant_id == env.default_id
            # The team's per-customer grant materializes exactly the same for either tid.
            assert await _admin_pairs(session, user.id) == {(env.a_id, "group")}
            assert await _auditor_pairs(session, user.id) == set()

    # (a) default tid and (b) customer A's tid -> byte-identical provider outcome.
    await _assert_provider_login(f"cell1-default-tid{_DOMAIN}", _TID_DEFAULT)
    await _assert_provider_login(f"cell1-customer-tid{_DOMAIN}", _TID_A)

    # (c) A customer-A-homed SSO account can only be produced via the ORM now; the reconcile's
    #     `is_provider_account` gate (first line, fail-closed) short-circuits it to a no-op even
    #     though its groups map provider Team T1 -> ZERO grants in either table. Never committed.
    async with get_session_factory()() as session:
        cust = AppUser(
            username=f"cell1-customer-homed{_DOMAIN}",
            password_hash="x",
            role="admin",
            is_sso=True,
            tenant_id=env.a_id,
        )
        session.add(cust)
        await session.flush()
        assert cust.id is not None
        await assignment_group_repo.reconcile_group_grants(session, cust, [_G1])
        assert cust.tenant_id == env.a_id  # homed at the CUSTOMER, not the default tenant
        assert await _admin_pairs(session, cust.id) == set()
        assert await _auditor_pairs(session, cust.id) == set()


# ========================================================================================= #
# CELL 2 -- MT SSO with NO matching Team is DENIED, whether the tid is the default or a
#   customer's, with NO side effects (no user, no session, no grant) and a `not_in_any_team`
#   LOGIN_FAILED audit. The customer leg carries A's SETTINGS-admin group to prove the settings
#   path is genuinely bypassed in multi-tenant mode.
# ========================================================================================= #


async def test_cell2_mt_sso_no_team_denied_no_side_effects(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, True)

    async def _assert_denied(username: str, tid: str, groups: list[str]) -> None:
        async with get_session_factory()() as session:
            resp = await _call_oidc_callback(session, _result(username, tid, groups))
        assert resp.status_code == 302
        assert "sso_denied=1" in resp.headers["location"]

        async with get_session_factory()() as session:
            assert await user_repo.get_by_username(session, username) is None
            row = (
                await session.execute(select(AuditLog).where(AuditLog.actor_username == username))
            ).scalar_one()
            assert row.action == LOGIN_FAILED
            assert row.outcome == "failure"
            assert row.detail.get("sso") is True
            assert row.detail.get("reason") == "not_in_any_team"
            # Fail-closed: no user exists, so no grant row can reference this username.
            for model in (AdminTenant, AuditorTenant):
                leaked = (
                    await session.execute(
                        select(model)
                        .join(AppUser, AppUser.id == model.user_id)
                        .where(AppUser.username == username)
                    )
                ).first()
                assert leaked is None

    # Default tid, a group matching no Team -> denied.
    await _assert_denied(f"cell2-default-no-team{_DOMAIN}", _TID_DEFAULT, ["tenref-not-a-team"])
    # Customer A's tid, in A's SETTINGS-admin group but NO Team -> STILL denied (no fallback).
    await _assert_denied(f"cell2-customer-settings{_DOMAIN}", _TID_A, [_A_SETTINGS_ADMIN])


# ========================================================================================= #
# CELL 3 -- Single-tenant SSO is byte-for-byte unaffected. read_mode OFF, tid=default ->
#   `resolve_role` (settings) decides: a Team-only member is DENIED (reason is NOT
#   `not_in_any_team` -- it comes from the settings path); a settings-admin member is allowed,
#   homed on default, role=admin, and gets NO group grants (teams are never reconciled here).
# ========================================================================================= #


async def test_cell3_single_tenant_sso_uses_settings_unchanged(
    env: _Env, migrated_engine: AsyncEngine
) -> None:
    await _write_mode(migrated_engine, env.default_id, False)  # multi-tenant OFF

    # (a) Team-only member (not in the settings-admin group) -> DENIED via the settings path.
    denied = f"cell3-team-only{_DOMAIN}"
    async with get_session_factory()() as session:
        resp = await _call_oidc_callback(session, _result(denied, _TID_DEFAULT, [_G1]))
    assert "sso_denied=1" in resp.headers["location"]
    async with get_session_factory()() as session:
        assert await user_repo.get_by_username(session, denied) is None
        row = (
            await session.execute(select(AuditLog).where(AuditLog.actor_username == denied))
        ).scalar_one()
        assert row.action == LOGIN_FAILED
        # Proves the team path is NOT taken in single-tenant mode.
        assert row.detail.get("reason") != "not_in_any_team"

    # (b) Settings-admin member -> allowed, role=admin, default-homed, ZERO group grants.
    ok = f"cell3-settings-admin{_DOMAIN}"
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
        assert await _admin_pairs(session, user.id) == set()
        assert await _auditor_pairs(session, user.id) == set()


# ========================================================================================= #
# CELL 4 -- Deprovision-delete fires ONLY under the full fail-safe gate (`sync_group`). Reuses
#   the Task-3 seeding/fake-Graph/table helpers verbatim, on the transactional `session`
#   fixture. The DELETE leg proves the delete path exists; each KEPT leg weakens exactly ONE
#   gate condition and would go RED if that condition were dropped. Assertions hit the
#   `app_user` / grant / `user_session` tables directly.
# ========================================================================================= #


async def test_cell4a_full_deprovision_deletes_account_and_sessions(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"cell4a-provider{_DOMAIN}"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None
    await _mk_session(session, p.id)

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (tenant_a.id, "group")
    }
    assert await _session_count(session, p.id) == 1

    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert upn not in await member_repo.upns_for_group(session, t_id)  # snapshot gone
    assert await _admin_rows(session, p.id) == []  # group grant revoked
    assert not await _user_exists(session, p.id)  # app_user DELETED
    assert await _session_count(session, p.id) == 0  # user_session rows gone


async def test_cell4b_manual_grant_blocks_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    tenant_b = await _mk_tenant(session)
    assert tenant_a.id is not None and tenant_b.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"cell4b-provider{_DOMAIN}"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None
    # Manual grant on a DISTINCT tenant B -- lives in the grant table, so it blocks the delete.
    await tenant_repo.add_grant(
        session, user_id=p.id, tenant_id=tenant_b.id, kind="admin", source="manual"
    )

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert {(r.tenant_id, r.source) for r in await _admin_rows(session, p.id)} == {
        (tenant_b.id, "manual")
    }
    assert await _user_exists(session, p.id)


async def test_cell4c_second_team_membership_blocks_deletion(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates gate condition (4) (`groups_containing_upn` non-empty) from (5) (holds a grant
    row): T2 is a ZERO-TENANT team, so P's T2 membership yields NO grant row, yet P still
    appears in T2's snapshot -- only condition (4) keeps P here. Drop (4) and P would delete."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])
    t2_id, t2 = await _mk_team(session, [])  # zero-tenant team -- membership only, no grant.

    upn = f"cell4c-provider{_DOMAIN}"
    p = await _mk_user(session, upn=upn, role="admin", tenant_id=default.id)
    assert p.id is not None

    _patch_graph(monkeypatch, {t: [_member(upn)], t2: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    await group_sync.sync_group(session, {}, t2_id)

    _patch_graph(monkeypatch, {t: [], t2: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)

    assert await member_repo.groups_containing_upn(session, upn)  # still in T2's snapshot
    assert await _user_exists(session, p.id)
    # And P holds NO grant row anywhere -- condition (5) alone would NOT have blocked the delete.
    assert await _admin_rows(session, p.id) == []
    assert await _auditor_rows(session, p.id) == []


async def test_cell4d_customer_homed_never_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    upn = f"cell4d-customer{_DOMAIN}"
    cust = await _mk_user(session, upn=upn, role="admin", tenant_id=tenant_a.id)
    assert cust.id is not None

    _patch_graph(monkeypatch, {t: [_member(upn)]})
    await group_sync.sync_group(session, {}, t_id)
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, cust.id)  # not a provider account -> never deleted.


async def test_cell4e_local_and_superadmin_collision_never_deleted(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOCAL account (condition 1) and a SUPERADMIN (conditions 1 + 2), each with a username
    that collides with an ex-member UPN and is default-homed, must both survive."""
    default = await tenant_repo.default_tenant(session)
    tenant_a = await _mk_tenant(session)
    assert tenant_a.id is not None
    t_id, t = await _mk_team(session, [tenant_a.id])

    local_upn = f"cell4e-local{_DOMAIN}"
    local = await _mk_user(session, upn=local_upn, role="admin", tenant_id=default.id, is_sso=False)
    su_upn = f"cell4e-root{_DOMAIN}"
    su = await _mk_user(session, upn=su_upn, role="superadmin", tenant_id=None, is_sso=False)
    assert local.id is not None and su.id is not None

    # Seed both UPNs into the snapshot, then sync an empty group so both are ex-member candidates.
    await member_repo.reconcile_snapshot(session, t_id, [_member(local_upn), _member(su_upn)])
    _patch_graph(monkeypatch, {t: []})
    await group_sync.sync_group(session, {}, t_id)

    assert await _user_exists(session, local.id)  # local -> condition (1) protects it.
    assert await _user_exists(session, su.id)  # superadmin -> conditions (1)+(2) protect it.


# ========================================================================================= #
# CELL 5 -- The sync-guard makes NO MSAL call. With Graph unconfigured, `execute_run` produces
#   status=='success', error is None, exactly one benign detail entry, and the GraphClient is
#   NEVER constructed (the `_boom_if_constructed` spy). Reuses the Task-1 run harness verbatim.
# ========================================================================================= #


async def test_cell5_sync_guard_makes_no_msal_call(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    dtid = await _real_default_tenant_id(migrated_engine)
    # Spy: constructing a GraphClient at all (which would begin the MSAL handshake) fails hard.
    monkeypatch.setattr(graph_sync, "GraphClient", _boom_if_constructed)
    _patch_everything_but_sync(monkeypatch)

    service = SchedulerService(open_active_session, base_url="http://test.local")
    run = await service.trigger_now(dry_run_override=True)

    try:
        assert run.tenant_id == dtid
        assert run.status == "success"
        assert run.error is None
        assert run.detail_log == [{"step": "sync", "skipped": "graph_not_configured"}], (
            f"the skip must appear EXACTLY once, benign, without an 'error' key: {run.detail_log}"
        )
    finally:
        async with migrated_engine.connect() as conn:
            await conn.execute(text("DELETE FROM run WHERE id = :rid"), {"rid": run.id})
            await conn.commit()


# ========================================================================================= #
# CELL 6 -- RLS backstop end-to-end. A provider account holding admin_tenant(A, group):
#   control plane -- is_allowed(P, A, write) True; a forged active-tenant for B (no B grant) is
#     denied (is_allowed False, read AND write);
#   data plane (RLS, REAL committed rows) -- a tenant-scoped session for A returns A's data and
#     ONLY A's; scoping to B never leaks A's rows.
# ========================================================================================= #


async def test_cell6_rls_backstop_end_to_end(env: _Env, migrated_engine: AsyncEngine) -> None:
    await _write_mode(migrated_engine, env.default_id, True)
    username = f"cell6-provider{_DOMAIN}"

    # Provider login via admin Team T1 -> admin_tenant(A, group).
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
            assert await _admin_pairs(session, user.id) == {(env.a_id, "group")}
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
