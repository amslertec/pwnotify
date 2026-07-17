"""Task 6 (Context-Gating v2, SECURITY-CRITICAL): adversarial security-verification matrix.

This is the ACCEPTANCE GATE for the whole increment (Tasks 1-5) -- it proves, against REAL
Postgres (so RLS + the app role actually participate, not the savepoint-isolated `session`
fixture used elsewhere), that no account ever reaches another customer's data or a
page/action outside its Matrix-B row. A red assertion here is a REAL finding in Tasks 1-5,
not a test bug.

**Carry-forward from Task 4 (opus review, IMPORTANT):** the existing
`test_matrix_b_route_gating.py::test_auditor_is_rejected_from_*` tests only called
`require_admin(auditor)` in isolation -- correct, but a weak regression lock, since it never
touches the ACTUAL route functions. Every auditor-403 assertion in this file drives the real
route bodies (`audit.list_audit`/`list_actions`, `settings.update`, `admin_users.create_local`/
`set_role`/`delete_user`) by reproducing FastAPI's own dependency-resolution order manually --
exactly the existing repo convention (see `test_matrix_b_route_gating.py`,
`test_admin_users_scoping.py`, `test_audit_tenant_scope.py`): resolve the guard/dependency the
route's parameter actually annotates, THEN call the route function with its result. Since the
guard raises first (as it must), the route body itself never executes for these negative
cases -- but the composed shape means a weakened/removed guard would make the very next line
actually invoke the real production route, which would then either succeed unexpectedly
(failing the surrounding `pytest.raises`) or fail for some unrelated reason -- either way the
regression is caught, unlike a bare isolated guard call.

**The nine account kinds** (Design SS2-4, Task-6-Brief):
1. Superadmin, default context -- instance/console/assignments succeed and are instance-wide
   (e.g. `/admin/tenants` sees A and B); `/access` (Access-Rescope), however, is scoped to the
   active (default) tenant even for the superadmin -- default-homed accounts + `superadmins`,
   never A's or B's.
2. Superadmin, switched into customer A -- operative view of A only; instance/console/
   assignments -> 403 `default_context_required`; A's data readable, B's not (RLS-scoped by
   the active claim, not a categorical block -- the superadmin COULD switch to B too, that is
   not an attack); `/access` likewise switches with it -- A's homed accounts only, no
   `superadmins` key.
3. Provider local admin (home=default, granted A) -- A read+write; B -> 403; no console.
4. Provider local auditor (home=default, granted A) -- A read-only; any write -> 403; no
   audit-of-B.
5. Provider SSO admin (home=default, granted A) -- home + grants only; no foreign customer.
6. Customer-A local admin (home=A) -- A only; B via every tenant-scoped route -> 403/empty;
   `/access` shows ONLY A's accounts, never B, never superadmins.
7. Customer-A local auditor (home=A) -- A read-only; `/audit` tenant-scoped to A, never B;
   settings-write/access-write -> 403.
8. Customer-A SSO admin (home=A) -- A only; cannot enumerate B via `/admin/tenants`.
9. Customer-A SSO auditor (home=A) -- A read-only; no B, no write, no audit-of-B.

Per kind, three properties (Task-6-Brief SS "assert THREE properties"):
(a) data isolation -- own-tenant rows only; a forged/switched `active_tenant` claim naming a
    FOREIGN tenant is REJECTED (403 `tenant_forbidden`), never a silent 0-rows leak.
(b) page/action reachability -- every route outside the kind's Matrix-B row -> 403.
(c) cross-grant immutability -- customer-homed kinds (6-9) can NEVER be granted a foreign
    tenant via `/admin/assignments` (`customer_account_not_grantable`), even by the
    superadmin.

Seed pattern (like `test_audit_tenant_scope.py`/`test_route_tenant_scoping.py`): a real,
committed Superuser connection on `migrated_engine` -- `get_tenant_session`/`get_audit_session`
open their OWN connection via `tenant_scoped_session`/`get_session_factory`, which would not
see uncommitted rows from the savepoint-isolated `session` fixture. Cleanup in `finally`,
residue-free, safe to run the whole suite twice.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from app.api.deps import (
    ACCESS_COOKIE,
    get_audit_session,
    get_current_user,
    get_tenant_session,
    require_admin,
    require_superadmin,
    require_superadmin_default_context,
)
from app.api.routes import (
    admin_assignments,
    admin_instance,
    admin_tenants,
    admin_users,
    audit,
    settings,
    users,
)
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.db.session import get_session_factory
from app.models.user import AppUser
from app.repositories import audit_repo, tenant_repo
from app.schemas.assignment import AssignmentUpdate
from app.schemas.auth import AdminUserCreate, RoleUpdate
from app.schemas.instance import InstanceUpdate
from app.schemas.settings import SettingsUpdate
from app.schemas.tenant import TenantCreate, TenantUpdate
from app.services.settings_service import SettingsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


# --------------------------------------------------------------------------------------- #
# Request/DI-chain helpers -- same shape as test_matrix_b_route_gating.py /
# test_audit_tenant_scope.py / test_active_tenant_resolution.py.
# --------------------------------------------------------------------------------------- #
class _FakeRequest:
    """Duck-typed Request -- guards/routes read only `.cookies` (audit calls in success
    paths additionally use `.headers`/`.client`, left empty/None like the existing
    `_FakeLoginRequest` convention)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _slug() -> str:
    return f"mx6-{uuid.uuid4().hex[:10]}"


def _req(user_id: int, active_tenant: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=active_tenant)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


@contextlib.asynccontextmanager
async def _owner_ctx(
    user_id: int, active_tenant: int | None = None
) -> AsyncGenerator[tuple[_FakeRequest, AppUser, AsyncSession]]:
    """SessionDep-only routes (`admin_tenants.list_tenants`, `admin_users.*`,
    `admin_instance.get_instance`) -- no RLS role switch, a real committed connection."""
    request = _req(user_id, active_tenant)
    async with get_session_factory()() as session:
        user = await get_current_user(request, session)
        yield request, user, session


@contextlib.asynccontextmanager
async def _tenant_ctx(
    user_id: int, active_tenant: int | None = None
) -> AsyncGenerator[tuple[_FakeRequest, AppUser, AsyncSession]]:
    """`TenantSessionDep` chain (`get_tenant_session`) -- for `users`/`runs`/`dashboard`/
    settings-write routes. Raises `ForbiddenError` here (before yielding) for a
    forged/foreign/inactive claim -- exactly the attack surface this file proves closed."""
    request = _req(user_id, active_tenant)
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_tenant_session(request, user, owner)
        try:
            scoped = await anext(gen)
            yield request, user, scoped
        finally:
            await gen.aclose()


@contextlib.asynccontextmanager
async def _audit_ctx(
    user_id: int, active_tenant: int | None = None
) -> AsyncGenerator[tuple[_FakeRequest, AppUser, AsyncSession]]:
    """`AuditSessionDep` chain (`get_audit_session`)."""
    request = _req(user_id, active_tenant)
    async with get_session_factory()() as owner:
        user = await get_current_user(request, owner)
        gen = get_audit_session(request, user, owner)
        try:
            scoped = await anext(gen)
            yield request, user, scoped
        finally:
            await gen.aclose()


# --------------------------------------------------------------------------------------- #
# Seed: default tenant (real) + customers A and B, each with local admin/auditor + SSO
# admin/auditor homed there, PLUS provider-homed (home=default) local admin/auditor/SSO
# admin granted A, PLUS a superadmin. B's accounts exist purely to make every isolation
# assertion NON-VACUOUS (B is genuinely populated, never just an empty control).
# --------------------------------------------------------------------------------------- #
@dataclass
class _Seed:
    default_id: int
    a_id: int
    b_id: int
    superadmin: AppUser
    provider_admin: AppUser
    provider_auditor: AppUser
    provider_sso_admin: AppUser
    a_admin: AppUser
    a_auditor: AppUser
    a_sso_admin: AppUser
    a_sso_auditor: AppUser
    b_admin: AppUser
    b_auditor: AppUser
    b_sso_admin: AppUser
    b_sso_auditor: AppUser
    a_audit_action: str
    b_audit_action: str
    a_entra_id: int
    b_entra_id: int
    a_run_id: int
    b_run_id: int
    extra_user_ids: list[int] = field(default_factory=list)
    """Accounts a test creates on the fly (e.g. via `create_local`) -- appended by the test,
    swept up by this fixture's own teardown so a failing assertion never leaks a row."""


@pytest_asyncio.fixture
async def seed(migrated_engine: AsyncEngine) -> AsyncGenerator[_Seed]:
    tag = uuid.uuid4().hex[:8]

    async def _account(
        conn: AsyncConnection, *, role: str, is_sso: bool, tenant_id: int | None
    ) -> int:
        row = await conn.execute(
            text(
                "INSERT INTO app_user (username, password_hash, role, is_active, is_sso, "
                "tenant_id, failed_login_count, language, created_at, updated_at) VALUES "
                "(:u, 'x', :r, true, :sso, :tid, 0, 'de', now(), now()) RETURNING id"
            ),
            {
                "u": f"mx6-{role}-{'sso' if is_sso else 'local'}-{tag}-{uuid.uuid4().hex[:6]}",
                "r": role,
                "sso": is_sso,
                "tid": tenant_id,
            },
        )
        return int(row.scalar_one())

    async with migrated_engine.connect() as conn:
        default_id = int(
            (await conn.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        )
        a_id, b_id = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO tenant (name, slug, is_active, created_at) VALUES "
                        f"('Mx6 Customer A {tag}', 'mx6-a-{tag}', true, now()), "
                        f"('Mx6 Customer B {tag}', 'mx6-b-{tag}', true, now()) RETURNING id"
                    )
                )
            )
            .scalars()
            .all()
        )
        a_id, b_id = int(a_id), int(b_id)

        superadmin_id = await _account(conn, role="superadmin", is_sso=False, tenant_id=default_id)
        provider_admin_id = await _account(conn, role="admin", is_sso=False, tenant_id=default_id)
        provider_auditor_id = await _account(
            conn, role="auditor", is_sso=False, tenant_id=default_id
        )
        provider_sso_admin_id = await _account(
            conn, role="admin", is_sso=True, tenant_id=default_id
        )
        a_admin_id = await _account(conn, role="admin", is_sso=False, tenant_id=a_id)
        a_auditor_id = await _account(conn, role="auditor", is_sso=False, tenant_id=a_id)
        a_sso_admin_id = await _account(conn, role="admin", is_sso=True, tenant_id=a_id)
        a_sso_auditor_id = await _account(conn, role="auditor", is_sso=True, tenant_id=a_id)
        b_admin_id = await _account(conn, role="admin", is_sso=False, tenant_id=b_id)
        b_auditor_id = await _account(conn, role="auditor", is_sso=False, tenant_id=b_id)
        b_sso_admin_id = await _account(conn, role="admin", is_sso=True, tenant_id=b_id)
        b_sso_auditor_id = await _account(conn, role="auditor", is_sso=True, tenant_id=b_id)

        # Explicit grants -- LOCAL accounts get NO home-union (only `is_sso` accounts do,
        # see `tenant_repo.admin_tenants`/`auditor_tenants`), so every local kind AND the
        # provider SSO admin (home=default, needs A on top of its home) need one here.
        grants = [
            ("admin_tenant", provider_admin_id, a_id),
            ("auditor_tenant", provider_auditor_id, a_id),
            ("admin_tenant", provider_sso_admin_id, a_id),
            ("admin_tenant", a_admin_id, a_id),
            ("auditor_tenant", a_auditor_id, a_id),
            ("admin_tenant", b_admin_id, b_id),
            ("auditor_tenant", b_auditor_id, b_id),
        ]
        for table, uid, tid in grants:
            await conn.execute(
                text(f"INSERT INTO {table} (user_id, tenant_id) VALUES (:u, :t)"),
                {"u": uid, "t": tid},
            )

        a_audit_action = f"test.mx6_a_event_{tag}"
        b_audit_action = f"test.mx6_b_event_{tag}"
        await conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, at, actor_type, action, outcome, detail) "
                "VALUES (:a, now(), 'system', :aa, 'success', '{}'::jsonb), "
                "(:b, now(), 'system', :ba, 'success', '{}'::jsonb)"
            ),
            {"a": a_id, "aa": a_audit_action, "b": b_id, "ba": b_audit_action},
        )

        a_entra_id, b_entra_id = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO entra_user (tenant_id, entra_id, upn, display_name, "
                        "other_mails, account_enabled, password_never_expires, excluded, "
                        "is_shared, raw, last_synced_at) VALUES "
                        f"(:a, 'mx6-a-entra-{tag}', 'mx6-a-{tag}@example.com', 'Mx6 A User', "
                        "'[]'::jsonb, true, false, false, false, '{}'::jsonb, now()), "
                        f"(:b, 'mx6-b-entra-{tag}', 'mx6-b-{tag}@example.com', 'Mx6 B User', "
                        "'[]'::jsonb, true, false, false, false, '{}'::jsonb, now()) "
                        "RETURNING id"
                    ),
                    {"a": a_id, "b": b_id},
                )
            )
            .scalars()
            .all()
        )
        a_entra_id, b_entra_id = int(a_entra_id), int(b_entra_id)

        a_run_id, b_run_id = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO run (tenant_id, trigger, dry_run, status, started_at, "
                        "checked_users, sent, failed, skipped, detail_log) VALUES "
                        "(:a, 'manual', false, 'ok', now(), 0, 0, 0, 0, '[]'::jsonb), "
                        "(:b, 'manual', false, 'ok', now(), 0, 0, 0, 0, '[]'::jsonb) "
                        "RETURNING id"
                    ),
                    {"a": a_id, "b": b_id},
                )
            )
            .scalars()
            .all()
        )
        a_run_id, b_run_id = int(a_run_id), int(b_run_id)

        await conn.commit()

        all_ids = [
            superadmin_id,
            provider_admin_id,
            provider_auditor_id,
            provider_sso_admin_id,
            a_admin_id,
            a_auditor_id,
            a_sso_admin_id,
            a_sso_auditor_id,
            b_admin_id,
            b_auditor_id,
            b_sso_admin_id,
            b_sso_auditor_id,
        ]

        def _u(uid: int, *, role: str, is_sso: bool, tenant_id: int | None) -> AppUser:
            return AppUser(
                id=uid,
                username=f"x{uid}",
                password_hash="x",
                role=role,
                is_sso=is_sso,
                tenant_id=tenant_id,
            )

        s = _Seed(
            default_id=default_id,
            a_id=a_id,
            b_id=b_id,
            superadmin=_u(superadmin_id, role="superadmin", is_sso=False, tenant_id=default_id),
            provider_admin=_u(provider_admin_id, role="admin", is_sso=False, tenant_id=default_id),
            provider_auditor=_u(
                provider_auditor_id, role="auditor", is_sso=False, tenant_id=default_id
            ),
            provider_sso_admin=_u(
                provider_sso_admin_id, role="admin", is_sso=True, tenant_id=default_id
            ),
            a_admin=_u(a_admin_id, role="admin", is_sso=False, tenant_id=a_id),
            a_auditor=_u(a_auditor_id, role="auditor", is_sso=False, tenant_id=a_id),
            a_sso_admin=_u(a_sso_admin_id, role="admin", is_sso=True, tenant_id=a_id),
            a_sso_auditor=_u(a_sso_auditor_id, role="auditor", is_sso=True, tenant_id=a_id),
            b_admin=_u(b_admin_id, role="admin", is_sso=False, tenant_id=b_id),
            b_auditor=_u(b_auditor_id, role="auditor", is_sso=False, tenant_id=b_id),
            b_sso_admin=_u(b_sso_admin_id, role="admin", is_sso=True, tenant_id=b_id),
            b_sso_auditor=_u(b_sso_auditor_id, role="auditor", is_sso=True, tenant_id=b_id),
            a_audit_action=a_audit_action,
            b_audit_action=b_audit_action,
            a_entra_id=a_entra_id,
            b_entra_id=b_entra_id,
            a_run_id=a_run_id,
            b_run_id=b_run_id,
        )
        try:
            yield s
        finally:
            cleanup_ids = all_ids + s.extra_user_ids
            await conn.execute(
                text("DELETE FROM audit_log WHERE action IN (:a, :b)"),
                {"a": a_audit_action, "b": b_audit_action},
            )
            await conn.execute(
                text("DELETE FROM run WHERE id IN (:a, :b)"), {"a": a_run_id, "b": b_run_id}
            )
            await conn.execute(
                text("DELETE FROM entra_user WHERE id IN (:a, :b)"),
                {"a": a_entra_id, "b": b_entra_id},
            )
            await conn.execute(
                text("DELETE FROM setting WHERE tenant_id IN (:a, :b)"), {"a": a_id, "b": b_id}
            )
            await conn.execute(
                text("DELETE FROM admin_tenant WHERE user_id = ANY(:ids)"), {"ids": cleanup_ids}
            )
            await conn.execute(
                text("DELETE FROM auditor_tenant WHERE user_id = ANY(:ids)"), {"ids": cleanup_ids}
            )
            await conn.execute(
                text("DELETE FROM user_session WHERE user_id = ANY(:ids)"), {"ids": cleanup_ids}
            )
            await conn.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"), {"ids": cleanup_ids}
            )
            await conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": a_id, "b": b_id}
            )
            await conn.commit()


# --------------------------------------------------------------------------------------- #
# Shared assertion helpers (kept small and reused across kinds instead of duplicated).
# --------------------------------------------------------------------------------------- #
async def _assert_no_superadmin_console(user: AppUser) -> None:
    """(b) reachability, provider-console side: every one of `admin_instance.update_instance`
    / the tenant CRUD console / `admin_assignments.*` annotates `SuperadminDefaultContextUser`,
    whose FIRST sub-dependency is `Depends(require_superadmin)` -- FastAPI resolves that
    before the route body (or the default-context check) ever runs, exactly as
    `test_matrix_b_route_gating.py::test_local_admin_is_rejected_by_superadmin_gate_first`
    established. One real, composed route call proves the whole family is locked."""
    request = _req(user.id or 0, None)
    async with get_session_factory()() as owner:
        with pytest.raises(ForbiddenError) as exc:
            guarded = await require_superadmin(user)
            await admin_tenants.create_tenant(
                request, guarded, TenantCreate(name="Should Not Exist", slug=_slug()), owner
            )
        assert exc.value.code == "superadmin_required"


async def _assert_auditor_locked_out_of_real_routes(
    auditor: AppUser, *, active_tenant: int, benign_target_id: int
) -> None:
    """Carry-forward fix (Task 4 review): drives `audit.list_audit`/`list_actions`, a real
    settings WRITE (`settings.update`), and a real access-management WRITE
    (`admin_users.create_local`/`set_role`/`delete_user`) -- NOT `require_admin` in
    isolation. Each composed call resolves `AdminUser`'s underlying dependency
    (`require_admin`) first, exactly as FastAPI would for that Annotated parameter."""
    request = _req(auditor.id or 0, active_tenant)

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            gen = get_audit_session(request, admin, owner)
            audit_session = await anext(gen)
            await audit.list_audit(admin, audit_session)
    assert exc.value.code == "admin_required"

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            gen = get_audit_session(request, admin, owner)
            audit_session = await anext(gen)
            await audit.list_actions(admin, audit_session)
    assert exc.value.code == "admin_required"

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            gen = get_tenant_session(request, admin, owner)
            tsession = await anext(gen)
            svc = SettingsService(tsession)
            await settings.update(
                request,
                admin,
                SettingsUpdate(values={"mail.from": "mx6-should-not@example.com"}),
                svc,
                tsession,
            )
    assert exc.value.code == "admin_required"

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            await admin_users.create_local(
                request,
                admin,
                AdminUserCreate(
                    username=f"mx6-should-not-{uuid.uuid4().hex[:8]}",
                    password="a-strong-password-1",
                    role="admin",
                ),
                owner,
                active_tenant,
            )
    assert exc.value.code == "admin_required"

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            await admin_users.set_role(
                request, admin, benign_target_id, RoleUpdate(role="auditor"), owner
            )
    assert exc.value.code == "admin_required"

    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(auditor)
        async with get_session_factory()() as owner:
            await admin_users.delete_user(request, admin, benign_target_id, owner)
    assert exc.value.code == "admin_required"


async def _forged_tenant_session_forbidden(user_id: int, foreign_tenant_id: int) -> None:
    """(a) forged/switched `active_tenant` claim naming a FOREIGN tenant -> 403
    `tenant_forbidden`, never a silent empty result."""
    with pytest.raises(ForbiddenError) as exc:
        async with _tenant_ctx(user_id, foreign_tenant_id):
            pass
    assert exc.value.code == "tenant_forbidden"


async def _forged_audit_session_forbidden(user_id: int, foreign_tenant_id: int) -> None:
    with pytest.raises(ForbiddenError) as exc:
        async with _audit_ctx(user_id, foreign_tenant_id):
            pass
    assert exc.value.code == "tenant_forbidden"


async def _entra_ids_visible(session: AsyncSession) -> set[int]:
    # `page`/`page_size` default to FastAPI `Query(...)` sentinels when the route is called
    # directly (bypassing DI) -- must be passed explicitly as plain ints here.
    out = await users.list_users(None, session, page=1, page_size=200)  # type: ignore[arg-type]
    return {item.id for item in out.items}


# ========================================================================================= #
# 1. Superadmin, DEFAULT context -- sees all; instance/console/assignments succeed.
# ========================================================================================= #
async def test_kind1_superadmin_default_context(seed: _Seed) -> None:
    """(Access-Rescope) `/access` is now scoped to the ACTIVE tenant for EVERY caller,
    superadmin included: in the DEFAULT context this means the default tenant's OWN homed
    accounts (provider staff) plus the instance-wide `superadmins` list -- NOT a global,
    all-tenants dump. `admin_tenants.list_tenants` remains instance-wide for the superadmin
    (a DIFFERENT route, not context-gated by design SS2) -- that assertion is unchanged."""
    superadmin = seed.superadmin
    assert superadmin.id is not None

    # (a) data isolation -- `/admin/tenants` sees BOTH A and B (unaffected, different route).
    async with _owner_ctx(superadmin.id, seed.default_id) as (_, user, session):
        tenants_out = await admin_tenants.list_tenants(user, session)
        tenant_ids = {t.id for t in tenants_out}
        assert seed.a_id in tenant_ids and seed.b_id in tenant_ids

        # `/access` in the DEFAULT context -- default-homed provider staff + `superadmins`,
        # NEVER A's or B's homed accounts (non-vacuous: A/B are genuinely populated).
        access_out = await admin_users.list_users(user, session, seed.default_id)
        assert "superadmins" in access_out
        superadmin_ids = {u.id for u in access_out["superadmins"]}
        assert superadmin.id in superadmin_ids

        local_ids = {u.id for u in access_out["local"]}
        sso_ids = {u.id for u in access_out["sso"]}
        assert seed.provider_admin.id in local_ids
        assert seed.provider_auditor.id in local_ids
        assert seed.provider_sso_admin.id in sso_ids
        assert seed.a_admin.id not in local_ids
        assert seed.b_admin.id not in local_ids
        assert seed.a_sso_admin.id not in sso_ids
        assert seed.b_sso_admin.id not in sso_ids

    # Audit: instance-wide (owner session, no RLS scoping) -- sees both A's and B's rows.
    async with _audit_ctx(superadmin.id, seed.default_id) as (_, admin_user, audit_session):
        page = await audit.list_audit(admin_user, audit_session, page=1, page_size=200, days=None)
    actions = {item.action for item in page.items}
    assert seed.a_audit_action in actions
    assert seed.b_audit_action in actions

    # (b) reachability: instance/console/assignments all succeed in the default context.
    request = _req(superadmin.id, seed.default_id)
    async with get_session_factory()() as owner:
        guarded = await require_superadmin_default_context(request, superadmin, owner)

        inst_out = await admin_instance.update_instance(
            request, guarded, InstanceUpdate(default_tenant_name="Mx6 Default Renamed"), owner
        )
        assert inst_out.default_tenant_name == "Mx6 Default Renamed"
        # Revert -- the default tenant's name is shared, real-world state other tests read.
        await admin_instance.update_instance(
            request, guarded, InstanceUpdate(default_tenant_name="Meine Firma"), owner
        )

        created = await admin_tenants.create_tenant(
            request, guarded, TenantCreate(name="Mx6 Console Co", slug=_slug()), owner
        )
        assert created.id is not None
        updated = await admin_tenants.update_tenant(
            request, guarded, created.id, TenantUpdate(name="Mx6 Console Co Renamed"), owner
        )
        assert updated.name == "Mx6 Console Co Renamed"
        deleted = await admin_tenants.delete_tenant(request, guarded, created.id, owner)
        assert deleted.message

        got = await admin_assignments.get_assignments(guarded, seed.a_admin.id, owner)
        assert got.tenant_ids == [seed.a_id]
        put = await admin_assignments.set_assignments(
            request,
            guarded,
            seed.provider_admin.id,
            AssignmentUpdate(tenant_ids=[seed.a_id]),
            owner,
        )
        assert seed.a_id in put.tenant_ids


# ========================================================================================= #
# 2. Superadmin, switched into customer A -- operative view of A only; provider console
#    locked; A readable, B not (this request's RLS scope, not a categorical block -- the
#    superadmin remains free to switch to B in a DIFFERENT request, that is not an attack).
# ========================================================================================= #
async def test_kind2_superadmin_switched_into_customer_a(seed: _Seed) -> None:
    superadmin = seed.superadmin
    assert superadmin.id is not None

    # (a) operative view: with active_tenant=A, tenant-scoped reads see ONLY A's row.
    async with _tenant_ctx(superadmin.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert seed.a_entra_id in ids
    assert seed.b_entra_id not in ids

    # Audit is likewise scoped to A while switched (superadmin only stays instance-wide
    # `not is_sso and role=="superadmin"` AND that branch never even resolves a claim --
    # but this composed call goes through the real `get_audit_session`, whose FIRST check
    # is exactly that instance-wide shortcut. To prove the SWITCHED superadmin's active
    # tenant genuinely governs `/audit` too would require a role without that shortcut --
    # already covered by every other kind below. Here we assert the documented shortcut:
    # the superadmin is instance-wide on `/audit` REGARDLESS of the switched context (by
    # design, Design SS2) -- this is NOT a leak: only a superadmin sees this, and Matrix B
    # gates the provider console, not `/audit` reads for the superadmin's own role.
    async with _audit_ctx(superadmin.id, seed.a_id) as (_, admin_user, audit_session):
        page = await audit.list_audit(admin_user, audit_session, page=1, page_size=200, days=None)
    actions = {item.action for item in page.items}
    assert seed.a_audit_action in actions and seed.b_audit_action in actions

    # (b) reachability: instance/console/assignments -> 403 default_context_required.
    request = _req(superadmin.id, seed.a_id)
    async with get_session_factory()() as owner:
        with pytest.raises(ForbiddenError) as exc:
            guarded = await require_superadmin_default_context(request, superadmin, owner)
            await admin_instance.update_instance(
                request, guarded, InstanceUpdate(default_tenant_name="Should Not Apply"), owner
            )
        assert exc.value.code == "default_context_required"

        with pytest.raises(ForbiddenError) as exc:
            guarded = await require_superadmin_default_context(request, superadmin, owner)
            await admin_tenants.create_tenant(
                request, guarded, TenantCreate(name="Should Not Exist", slug=_slug()), owner
            )
        assert exc.value.code == "default_context_required"

        with pytest.raises(ForbiddenError) as exc:
            guarded = await require_superadmin_default_context(request, superadmin, owner)
            await admin_assignments.set_assignments(
                request, guarded, seed.a_admin.id, AssignmentUpdate(tenant_ids=[seed.a_id]), owner
            )
        assert exc.value.code == "default_context_required"

        # `/admin/tenants` itself is NOT a provider-console route (`CurrentUser`, not
        # `SuperadminDefaultContextUser`) -- the superadmin still enumerates every tenant,
        # switched context or not (Design SS2: only the CONSOLE ACTIONS are context-gated).
        tenants_out = await admin_tenants.list_tenants(superadmin, owner)
        assert {seed.a_id, seed.b_id} <= {t.id for t in tenants_out}

    # `/access` (Access-Rescope), by contrast, IS scoped to the active tenant for every
    # caller, superadmin included: switched into A, it returns ONLY A's homed accounts --
    # no `superadmins` key (that only appears in the DEFAULT context, see kind 1), never
    # B's accounts, never the default context's provider staff.
    async with _owner_ctx(superadmin.id, seed.a_id) as (_, current_user, session):
        access_out = await admin_users.list_users(current_user, session, seed.a_id)
    assert "superadmins" not in access_out
    local_ids = {u.id for u in access_out["local"]}
    sso_ids = {u.id for u in access_out["sso"]}
    assert seed.a_admin.id in local_ids
    assert seed.a_auditor.id in local_ids
    assert seed.a_sso_admin.id in sso_ids
    assert seed.a_sso_auditor.id in sso_ids
    assert seed.b_admin.id not in local_ids
    assert seed.b_sso_admin.id not in sso_ids
    assert seed.provider_admin.id not in local_ids
    assert seed.superadmin.id not in local_ids


# ========================================================================================= #
# 3. Provider local admin (home=default, granted A) -- A read+write; B -> 403; no console.
# ========================================================================================= #
async def test_kind3_provider_local_admin(seed: _Seed) -> None:
    user = seed.provider_admin
    assert user.id is not None

    # (a) data isolation: A read succeeds and sees only A; forged claim on B -> 403.
    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    await _forged_tenant_session_forbidden(user.id, seed.b_id)

    # (a) write success on A (real settings.update route).
    async with _tenant_ctx(user.id, seed.a_id) as (request, current_user, tsession):
        admin = await require_admin(current_user)
        svc = SettingsService(tsession)
        out = await settings.update(
            request,
            admin,
            SettingsUpdate(values={"mail.from": "mx6-provider-a@example.com"}),
            svc,
            tsession,
        )
    assert out["mail.from"] == "mx6-provider-a@example.com"

    # (a) write REJECTED on foreign B, even though the account holds admin capacity.
    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(user)  # role gate passes
        async with _tenant_ctx(user.id, seed.b_id):  # tenant gate must not
            pass
    assert exc.value.code == "tenant_forbidden"

    # (b) no console/instance -- non-superadmin.
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 4. Provider local auditor (home=default, granted A) -- A read-only; any write -> 403;
#    no audit-of-B.
# ========================================================================================= #
async def test_kind4_provider_local_auditor(seed: _Seed) -> None:
    user = seed.provider_auditor
    assert user.id is not None

    # (a) A read succeeds; audit scoped to A only; foreign B claim on audit -> 403.
    async with _audit_ctx(user.id, seed.a_id) as (_, admin_stub, audit_session):
        # `_: AdminUser` is not this account's gate for reads via get_audit_session itself --
        # the AUDIT ROUTE's `AdminUser` param is what blocks it, exercised below via the
        # shared carry-forward helper. Here we drive `get_audit_session` alone to prove the
        # SESSION SCOPING (not the route-level role gate) is also correct for an auditor.
        del admin_stub
        rows, _total = await audit_repo.list_paged(audit_session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert seed.a_audit_action in actions
    assert seed.b_audit_action not in actions
    await _forged_audit_session_forbidden(user.id, seed.b_id)

    # (b) real audit/settings-write/access-write routes reject this auditor (carry-forward).
    assert seed.a_admin.id is not None
    await _assert_auditor_locked_out_of_real_routes(
        user, active_tenant=seed.a_id, benign_target_id=seed.a_admin.id
    )

    # (b) no console/instance -- non-superadmin.
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 5. Provider SSO admin (home=default, granted A) -- home + grants only; no foreign customer.
# ========================================================================================= #
async def test_kind5_provider_sso_admin(seed: _Seed) -> None:
    user = seed.provider_sso_admin
    assert user.id is not None

    # (a) A (the grant) is reachable read+write.
    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    async with _tenant_ctx(user.id, seed.a_id) as (request, current_user, tsession):
        admin = await require_admin(current_user)
        svc = SettingsService(tsession)
        out = await settings.update(
            request,
            admin,
            SettingsUpdate(values={"mail.from": "mx6-provider-sso@example.com"}),
            svc,
            tsession,
        )
    assert out["mail.from"] == "mx6-provider-sso@example.com"

    # (a) B is foreign -- neither home nor grant -- 403.
    await _forged_tenant_session_forbidden(user.id, seed.b_id)

    # (b) no console/instance -- non-superadmin.
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 6. Customer-A local admin (home=A) -- A only; B via every tenant-scoped route -> 403/
#    empty; `/access` shows ONLY A's accounts, never B, never superadmins.
# ========================================================================================= #
async def test_kind6_customer_a_local_admin(seed: _Seed) -> None:
    user = seed.a_admin
    assert user.id is not None

    # (a) data isolation on tenant-data routes.
    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    await _forged_tenant_session_forbidden(user.id, seed.b_id)

    # (a) write success on A, rejected on B.
    async with _tenant_ctx(user.id, seed.a_id) as (request, current_user, tsession):
        admin = await require_admin(current_user)
        svc = SettingsService(tsession)
        out = await settings.update(
            request,
            admin,
            SettingsUpdate(values={"mail.from": "mx6-a-admin@example.com"}),
            svc,
            tsession,
        )
    assert out["mail.from"] == "mx6-a-admin@example.com"
    with pytest.raises(ForbiddenError) as exc:
        admin = await require_admin(user)
        async with _tenant_ctx(user.id, seed.b_id):
            pass
    assert exc.value.code == "tenant_forbidden"

    # (a) `/admin/tenants` -- A only, never B.
    async with _owner_ctx(user.id, seed.a_id) as (_, current_user, session):
        tenants_out = await admin_tenants.list_tenants(current_user, session)
        tenant_ids = {t.id for t in tenants_out}
    assert tenant_ids == {seed.a_id}

    # (a) `/access` -- A's own accounts, NEVER B, NEVER superadmins.
    async with _owner_ctx(user.id, seed.a_id) as (_, current_user, session):
        access_out = await admin_users.list_users(current_user, session, seed.a_id)
    assert "superadmins" not in access_out
    all_ids = {u.id for u in access_out["local"]} | {u.id for u in access_out["sso"]}
    assert seed.a_admin.id in all_ids
    assert seed.a_auditor.id in all_ids
    assert seed.a_sso_admin.id in all_ids
    assert seed.a_sso_auditor.id in all_ids
    assert seed.b_admin.id not in all_ids
    assert seed.b_auditor.id not in all_ids
    assert seed.b_sso_admin.id not in all_ids
    assert seed.b_sso_auditor.id not in all_ids
    assert seed.superadmin.id not in all_ids

    # (b) no console/instance -- non-superadmin.
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 7. Customer-A local auditor (home=A) -- A read-only; `/audit` scoped to A, never B;
#    settings-write/access-write -> 403.
# ========================================================================================= #
async def test_kind7_customer_a_local_auditor(seed: _Seed) -> None:
    user = seed.a_auditor
    assert user.id is not None

    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    await _forged_tenant_session_forbidden(user.id, seed.b_id)
    await _forged_audit_session_forbidden(user.id, seed.b_id)

    async with _audit_ctx(user.id, seed.a_id) as (_, _, audit_session):
        rows, _total = await audit_repo.list_paged(audit_session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert seed.a_audit_action in actions
    assert seed.b_audit_action not in actions

    assert seed.a_admin.id is not None
    await _assert_auditor_locked_out_of_real_routes(
        user, active_tenant=seed.a_id, benign_target_id=seed.a_admin.id
    )
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 8. Customer-A SSO admin (home=A) -- A only; cannot enumerate B via `/admin/tenants`.
# ========================================================================================= #
async def test_kind8_customer_a_sso_admin(seed: _Seed) -> None:
    user = seed.a_sso_admin
    assert user.id is not None

    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    await _forged_tenant_session_forbidden(user.id, seed.b_id)

    async with _owner_ctx(user.id, seed.a_id) as (_, current_user, session):
        tenants_out = await admin_tenants.list_tenants(current_user, session)
        tenant_ids = {t.id for t in tenants_out}
    assert tenant_ids == {seed.a_id}
    assert seed.b_id not in tenant_ids

    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# 9. Customer-A SSO auditor (home=A) -- A read-only; no B, no write, no audit-of-B.
# ========================================================================================= #
async def test_kind9_customer_a_sso_auditor(seed: _Seed) -> None:
    user = seed.a_sso_auditor
    assert user.id is not None

    async with _tenant_ctx(user.id, seed.a_id) as (_, _, tsession):
        ids = await _entra_ids_visible(tsession)
    assert ids == {seed.a_entra_id}
    await _forged_tenant_session_forbidden(user.id, seed.b_id)
    await _forged_audit_session_forbidden(user.id, seed.b_id)

    async with _audit_ctx(user.id, seed.a_id) as (_, _, audit_session):
        rows, _total = await audit_repo.list_paged(audit_session, page=1, page_size=200)
    actions = {r.action for r in rows}
    assert seed.a_audit_action in actions
    assert seed.b_audit_action not in actions

    assert seed.a_admin.id is not None
    await _assert_auditor_locked_out_of_real_routes(
        user, active_tenant=seed.a_id, benign_target_id=seed.a_admin.id
    )
    await _assert_no_superadmin_console(user)


# ========================================================================================= #
# (c) Cross-grant immutability -- customer-homed kinds (6-9) can NEVER be granted a foreign
#     tenant via `/admin/assignments`, even by the superadmin, in the DEFAULT context.
# ========================================================================================= #
async def test_cross_grant_immutability_for_all_customer_homed_kinds(seed: _Seed) -> None:
    superadmin = seed.superadmin
    assert superadmin.id is not None
    request = _req(superadmin.id, seed.default_id)

    customer_homed = [seed.a_admin, seed.a_auditor, seed.a_sso_admin, seed.a_sso_auditor]
    async with get_session_factory()() as owner:
        guarded = await require_superadmin_default_context(request, superadmin, owner)
        for target in customer_homed:
            assert target.id is not None
            with pytest.raises(ForbiddenError) as exc:
                await admin_assignments.set_assignments(
                    request, guarded, target.id, AssignmentUpdate(tenant_ids=[seed.b_id]), owner
                )
            assert exc.value.code == "customer_account_not_grantable"

            # No partial write -- the target's grant set on B is empty afterwards.
            kind = "admin" if target.role == "admin" else "auditor"
            remaining = await tenant_repo.list_grant_tenant_ids(owner, target.id, kind)
            assert seed.b_id not in remaining

        # Positive control: the SAME lock does not block granting a customer-homed account
        # its OWN home tenant (A) -- the lock is scoped to FOREIGN grants specifically, not
        # a blanket rejection of the entire route for customer-homed targets.
        target = seed.a_admin
        assert target.id is not None
        out = await admin_assignments.set_assignments(
            request, guarded, target.id, AssignmentUpdate(tenant_ids=[seed.a_id]), owner
        )
        assert out.tenant_ids == [seed.a_id]
