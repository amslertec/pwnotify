"""Security Phase 5, Task 7 (M11): tenant-attributable stamping of owner-session audit
entries.

`AuditLog.tenant_id` uses `default_factory=current_tenant_or_none` (`app/models/audit.py`),
which correctly stamps the active tenant on tenant-scoped sessions. The remaining gap:
owner-session routes with no active `ContextVar` (`admin_users.create_local`/`set_role`/
`delete_user`/`send_reset`) always wrote `tenant_id=NULL`, even though the action is clearly
attributable to one customer -- making these entries invisible to that customer's own
admin/auditor (RLS-scoped reads only see rows where `tenant_id` matches their tenant).

This test proves, real-commit (owner-session routes commit internally, and RLS visibility
can only be proven from a SEPARATE connection -- a savepoint-only fixture would not do):

1. A local admin, homed and granted on tenant A, calls `create_local`/`set_role` (handler
   level, not HTTP) against an A-homed account -> the written `audit_log` row carries
   `tenant_id == A` (before the fix: NULL -- this assertion is RED without it).
2. That same row is actually VISIBLE to an A-scoped reader (`tenant_scoped_session(A)` +
   `audit_repo.list_paged`) -- proving the RLS-visibility consequence, not just the raw
   column value.
3. Negative control (non-vacuous scope boundary): `create_superadmin`, a provider-level
   action with no single customer to attribute to, still writes `tenant_id IS NULL`.

All rows are real-committed via a plain owner session (`get_session_factory()`, same pattern
as `test_audit_tenant_scope.py`'s `_audit_session_for`) and cleaned up FK-safely in `finally`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.api.deps import default_tenant_id
from app.api.routes.admin_users import (
    create_local,
    create_superadmin,
    delete_user,
    send_reset,
    set_role,
)
from app.db.session import get_session_factory
from app.db.tenant_context import tenant_scoped_session
from app.repositories import audit_repo, tenant_repo, user_repo
from app.schemas.auth import AdminUserCreate, RoleUpdate, SuperadminCreate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class _FakeSender:
    """No-op mail sender (Review-Fix test below): `create_superadmin`'s invite path and
    `send_reset` both dispatch real mail via `services.user_token._send` -- faked here the
    same way `test_password_reset_flow.py` does, since this module's real-commit sessions
    are not the savepoint-isolated `session` fixture that other suites rely on."""

    backend = "fake"

    async def send(self, **kwargs: Any) -> None:
        return None


def _slug() -> str:
    return f"t7attr-{uuid.uuid4().hex[:10]}"


def _uname(label: str) -> str:
    return f"t7attr-{label}-{uuid.uuid4().hex[:8]}"


async def test_owner_session_user_management_actions_stamp_home_tenant_and_stay_rls_visible(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        tenant = await tenant_repo.create(session, name="T7 Attribution Tenant", slug=_slug())
        assert tenant.id is not None
        tenant_id = tenant.id

        admin = await user_repo.create(
            session,
            username=_uname("admin"),
            password_hash="x",
            role="admin",
            is_sso=False,
            tenant_id=tenant_id,
        )
        assert admin.id is not None
        await tenant_repo.add_grant(session, user_id=admin.id, tenant_id=tenant_id, kind="admin")

        superadmin = await user_repo.create(
            session, username=_uname("superadmin"), password_hash="x", role="superadmin"
        )

        created_user_ids: list[int] = [admin.id]
        try:
            # --- create_local: admin of A creates a new A-homed account ---
            create_body = AdminUserCreate(
                username=_uname("target"), password="Str0ng!Passw0rd1", role="admin"
            )
            target_out = await create_local(None, admin, create_body, session, tenant_id)  # type: ignore[arg-type]
            assert target_out.id is not None
            created_user_ids.append(target_out.id)

            created_row = (
                await session.execute(
                    text(
                        "SELECT tenant_id FROM audit_log "
                        "WHERE action = 'user.created' AND target = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": target_out.username},
                )
            ).one()
            assert created_row.tenant_id == tenant_id, (
                "create_local's audit row was not stamped with the target's home tenant "
                "(RED before the fix -- owner-session insert left tenant_id NULL)"
            )

            # --- set_role: same admin promotes/demotes the same A-homed account ---
            await set_role(  # type: ignore[arg-type]
                None, admin, target_out.id, RoleUpdate(role="auditor"), session
            )
            role_changed_row = (
                await session.execute(
                    text(
                        "SELECT tenant_id FROM audit_log "
                        "WHERE action = 'user.role_changed' AND target = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": target_out.username},
                )
            ).one()
            assert role_changed_row.tenant_id == tenant_id, (
                "set_role's audit row was not stamped with the target's home tenant"
            )

            # --- RLS-visibility proof: an A-scoped reader must see BOTH rows now ---
            async with tenant_scoped_session(tenant_id) as tsession:
                rows, _total = await audit_repo.list_paged(tsession, page=1, page_size=200)
            visible_actions = {(r.action, r.target) for r in rows}
            assert ("user.created", target_out.username) in visible_actions, (
                "A-scoped auditor still cannot see the create_local entry (RLS-invisible)"
            )
            assert ("user.role_changed", target_out.username) in visible_actions, (
                "A-scoped auditor still cannot see the set_role entry (RLS-invisible)"
            )

            # --- Negative control: create_superadmin is provider-level, stays NULL ---
            sa_body = SuperadminCreate(username=_uname("newsa"), password="Str0ng!Passw0rd1")
            sa_out = await create_superadmin(None, superadmin, sa_body, session)  # type: ignore[arg-type]
            assert sa_out.id is not None
            created_user_ids.append(sa_out.id)

            sa_row = (
                await session.execute(
                    text(
                        "SELECT tenant_id FROM audit_log "
                        "WHERE action = 'user.superadmin_created' AND target = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": sa_out.username},
                )
            ).one()
            assert sa_row.tenant_id is None, (
                "create_superadmin (provider-level) must never be tenant-stamped -- "
                "non-vacuous scope boundary"
            )
        finally:
            created_user_ids.append(superadmin.id)  # type: ignore[arg-type]
            await session.execute(
                text("DELETE FROM audit_log WHERE actor_id = ANY(:ids)"),
                {"ids": created_user_ids},
            )
            await session.execute(
                text("DELETE FROM admin_tenant WHERE user_id = ANY(:ids)"),
                {"ids": created_user_ids},
            )
            await session.execute(
                text("DELETE FROM auditor_tenant WHERE user_id = ANY(:ids)"),
                {"ids": created_user_ids},
            )
            await session.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"), {"ids": created_user_ids}
            )
            await session.execute(text("DELETE FROM tenant WHERE id = :tid"), {"tid": tenant_id})
            await session.commit()


async def test_superadmin_lifecycle_events_never_attribute_to_the_default_tenant(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review-Fix (Task 7/M11 follow-up, Important finding): an INVITE-created superadmin
    carries `tenant_id = Default-Tenant` -- a branding anchor for the invite mail only (see
    `create_superadmin`'s docstring), never a real membership. Both `delete_user` (a
    superadmin caller may remove any non-last superadmin) and `send_reset` (no superadmin
    guard at all) can target such an account. Before the fix, both stamped
    `tenant_id=target.tenant_id` unconditionally -- leaking a provider-level superadmin
    lifecycle event into the Default-Tenant's own audit view, visible to any admin/auditor
    scoped to that tenant. `set_role` is NOT covered here: it hard-rejects any
    `target.role == "superadmin"` before ever reaching its `tenant_id` stamp (see the guard
    at the top of that route), so a superadmin target can never reach that line.
    """
    import app.services.user_token as user_token_service

    monkeypatch.setattr(user_token_service, "build_sender", lambda _settings: _FakeSender())

    session_factory = get_session_factory()
    async with session_factory() as session:
        caller = await user_repo.create(
            session, username=_uname("caller"), password_hash="x", role="superadmin"
        )
        assert caller.id is not None
        # `count_superadmins()` only counts ACTIVE superadmins (Design §11.4); an invited
        # target stays `is_active=False` until accepted, so it never counts itself -- without
        # a SECOND active superadmin here, `caller` alone (count == 1) would trip the
        # last-superadmin guard on `delete_user` below, unrelated to what this test proves.
        extra_superadmin = await user_repo.create(
            session, username=_uname("extra"), password_hash="x", role="superadmin"
        )
        assert extra_superadmin.id is not None

        default_tid = await default_tenant_id(session)

        created_user_ids: list[int] = [caller.id, extra_superadmin.id]
        try:
            # --- delete_user target: invited superadmin, tenant_id == Default-Tenant ---
            del_body = SuperadminCreate(email=f"{_uname('del')}@t7attr.test")
            del_target = await create_superadmin(None, caller, del_body, session)  # type: ignore[arg-type]
            assert del_target.id is not None
            created_user_ids.append(del_target.id)

            precondition = (
                await session.execute(
                    text("SELECT tenant_id, role FROM app_user WHERE id = :id"),
                    {"id": del_target.id},
                )
            ).one()
            assert precondition.role == "superadmin"
            assert precondition.tenant_id == default_tid, (
                "precondition: an invited superadmin is homed on the Default-Tenant "
                "(branding anchor only, not a real membership)"
            )

            await delete_user(None, caller, del_target.id, session)  # type: ignore[arg-type]
            del_row = (
                await session.execute(
                    text(
                        "SELECT tenant_id FROM audit_log "
                        "WHERE action = 'user.deleted' AND target = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": del_target.username},
                )
            ).one()
            assert del_row.tenant_id is None, (
                "delete_user on a superadmin target stamped the Default-Tenant instead of "
                "NULL (RED without the fix -- leaks a provider-level event into the "
                "Default-Tenant's audit view)"
            )

            # --- send_reset target: a SEPARATE invited superadmin (delete_user above already
            # consumed its own target) ---
            reset_body = SuperadminCreate(email=f"{_uname('reset')}@t7attr.test")
            reset_target = await create_superadmin(None, caller, reset_body, session)  # type: ignore[arg-type]
            assert reset_target.id is not None
            created_user_ids.append(reset_target.id)

            await send_reset(None, caller, reset_target.id, session)  # type: ignore[arg-type]
            reset_row = (
                await session.execute(
                    text(
                        "SELECT tenant_id FROM audit_log "
                        "WHERE action = 'auth.password_reset_sent' AND target = :t "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": reset_target.username},
                )
            ).one()
            assert reset_row.tenant_id is None, (
                "send_reset on a superadmin target stamped the Default-Tenant instead of "
                "NULL (RED without the fix)"
            )
        finally:
            # `user_token.created_by` does NOT cascade on the creator's deletion (an invite/
            # reset token issued by a since-deleted admin must survive) -- must clear rows
            # created BY the caller before the caller's own `app_user` row goes away.
            # `app_user_id` DOES cascade, so the targets' own tokens need no separate delete.
            await session.execute(
                text("DELETE FROM user_token WHERE created_by = ANY(:ids)"),
                {"ids": created_user_ids},
            )
            await session.execute(
                text("DELETE FROM audit_log WHERE actor_id = ANY(:ids)"),
                {"ids": created_user_ids},
            )
            await session.execute(
                text("DELETE FROM app_user WHERE id = ANY(:ids)"), {"ids": created_user_ids}
            )
            await session.commit()
