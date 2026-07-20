"""DB access for local UI accounts and refresh sessions."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.tenant import AdminTenant, AuditorTenant
from ..models.token import UserToken
from ..models.user import AppUser, UserSession


# ---- AppUser ---------------------------------------------------------------- #
async def get_by_username(session: AsyncSession, username: str) -> AppUser | None:
    res = await session.execute(select(AppUser).where(AppUser.username == username))
    return res.scalar_one_or_none()


async def get(session: AsyncSession, user_id: int) -> AppUser | None:
    return await session.get(AppUser, user_id)


async def count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(AppUser.id)))).scalar_one())


async def count_admins(session: AsyncSession) -> int:
    """Active administrators (local + SSO). Basis for the lockout protection.

    Deliberately does NOT count the `superadmin` role -- the superadmin is protected
    from lockout separately via `count_superadmins` (Task 4: the last superadmin may
    neither be deleted nor demoted)."""
    stmt = select(func.count(AppUser.id)).where(
        AppUser.role == "admin", AppUser.is_active.is_(True)
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_tenant_admins(session: AsyncSession, tenant_id: int) -> int:
    """Active accounts with admin (write) capacity on EXACTLY this tenant -- basis for the
    per-customer lockout protection (A4), the per-tenant counterpart to `count_admins`.

    Counts congruently with `tenant_repo.admin_tenants(user)` (Design §2):
    - Accounts (local OR SSO) with an `admin_tenant` grant row on `tenant_id`; PLUS
    - SSO accounts whose HOME tenant (`AppUser.tenant_id`) is exactly this one AND whose role
      is `admin` -- their home grants admin capacity without their own grant row.

    Deliberately EXCLUDED:
    - **Superadmins** (`role=='superadmin'`): instance-wide, they administer all tenants
      anyway and are protected from lockout separately via `count_superadmins` -- a (stray)
      `admin_tenant` grant on a superadmin must not mask the last customer admin.
    - **Inactive/pending accounts** (`is_active=False`): a deactivated or not-yet-accepted
      (invited) account cannot administer anyone and therefore does not count as a
      remaining admin.

    `func.distinct` because the LEFT JOIN on `admin_tenant` would otherwise count an account
    with multiple grant rows more than once (filtered here on `tenant_id` it would be at most
    one row -- the composite PK guarantees that -- but `distinct` remains the defensive
    expression of the intent "accounts, not rows")."""
    stmt = (
        select(func.count(func.distinct(AppUser.id)))
        .select_from(AppUser)
        .outerjoin(AdminTenant, AdminTenant.user_id == AppUser.id)
        .where(
            AppUser.is_active.is_(True),
            AppUser.role != "superadmin",
            or_(
                AdminTenant.tenant_id == tenant_id,
                and_(
                    AppUser.is_sso.is_(True),
                    AppUser.role == "admin",
                    AppUser.tenant_id == tenant_id,
                ),
            ),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_superadmins(session: AsyncSession) -> int:
    """Active superadmins (always local, `is_sso=False`). Basis for the
    last-superadmin protection (Task 4) -- analogous to `count_admins`, but for the
    instance-wide role."""
    stmt = select(func.count(AppUser.id)).where(
        AppUser.role == "superadmin", AppUser.is_active.is_(True)
    )
    return int((await session.execute(stmt)).scalar_one())


async def create(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    role: str = "admin",
    display_name: str | None = None,
    is_sso: bool = False,
    tenant_id: int | None = None,
) -> AppUser:
    user = AppUser(
        username=username,
        password_hash=password_hash,
        role=role,
        display_name=display_name,
        is_sso=is_sso,
        tenant_id=tenant_id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def list_all(session: AsyncSession) -> list[AppUser]:
    res = await session.execute(select(AppUser).order_by(AppUser.username))
    return list(res.scalars().all())


async def list_sso_for_tenant(session: AsyncSession, tenant_id: int) -> list[AppUser]:
    """SSO users of ONLY this tenant -- basis for the sync reconciliation

    (security fix: an instance-wide SSO list would make `sync_sso_users` build the
    removal set across ALL customers, so SSO accounts of OTHER customers -- including
    their admins -- would wrongly appear as "no longer in any group" and get deleted as
    soon as a second SSO customer exists. See `sync_sso_users` in `services/oidc.py`).
    """
    res = await session.execute(
        select(AppUser).where(AppUser.is_sso.is_(True), AppUser.tenant_id == tenant_id)
    )
    return list(res.scalars().all())


async def list_sso_in_tenants(session: AsyncSession, tenant_ids: set[int]) -> list[AppUser]:
    """SSO accounts whose home `tenant_id` is in `tenant_ids` -- basis for the
    scoped access page of a local admin (Task 3): they may see ONLY SSO accounts of
    the customers they themselves hold (`tenant_repo.admin_tenants`), never the full,
    instance-wide SSO list."""
    if not tenant_ids:
        return []
    res = await session.execute(
        select(AppUser)
        .where(AppUser.is_sso.is_(True), AppUser.tenant_id.in_(tenant_ids))
        .order_by(AppUser.username)
    )
    return list(res.scalars().all())


async def list_local_homed_in_tenant(session: AsyncSession, tenant_id: int) -> list[AppUser]:
    """Local (non-SSO) non-superadmin accounts whose HOME (`tenant_id`) is exactly the
    active tenant -- basis for the access rescope (security fix): the access page
    shows every caller (superadmin included) ONLY the HOME accounts of the respectively
    ACTIVE tenant, never a union of assignments (`list_local_granted_to_tenants`, which
    still applies for grants, see below) and never an instance-wide or other tenant's list.
    ALWAYS excludes superadmins (`role != 'superadmin'`) -- they are instance-wide and
    belong to no customer home; their own list is delivered separately by `list_users`
    only in the DEFAULT context (`superadmins` key)."""
    res = await session.execute(
        select(AppUser)
        .where(
            AppUser.is_sso.is_(False),
            AppUser.tenant_id == tenant_id,
            AppUser.role != "superadmin",
        )
        .order_by(AppUser.username)
    )
    return list(res.scalars().all())


async def list_local_granted_to_tenants(
    session: AsyncSession, tenant_ids: set[int]
) -> list[AppUser]:
    """Local (non-SSO) admins/auditors with an `admin_tenant` OR
    `auditor_tenant` assignment on one of the `tenant_ids` -- counterpart to
    `list_sso_in_tenants` for local accounts on the scoped access page (Task 3).

    ALWAYS excludes superadmins (`role != 'superadmin'`) -- they are instance-wide and
    must never be shown to a local admin, even if (which should not happen)
    some assignment row existed."""
    if not tenant_ids:
        return []
    res = await session.execute(
        select(AppUser)
        .where(
            AppUser.is_sso.is_(False),
            AppUser.role != "superadmin",
            or_(
                AppUser.id.in_(
                    select(AdminTenant.user_id).where(AdminTenant.tenant_id.in_(tenant_ids))
                ),
                AppUser.id.in_(
                    select(AuditorTenant.user_id).where(AuditorTenant.tenant_id.in_(tenant_ids))
                ),
            ),
        )
        .order_by(AppUser.username)
    )
    return list(res.scalars().all())


async def delete(session: AsyncSession, user_id: int) -> None:
    """Stage the removal of the account and its sessions; the CALLER must commit.

    Deliberately does NOT commit (M-03): the deletion and the caller's `USER_DELETED`
    audit entry must land in ONE transaction. If this committed internally, a crash
    between the internal commit and the caller's audit commit would persist the removal
    while losing its audit trail (a silent deletion). Every caller (`admin_users.
    delete_user`, `group_sync.sync_group`, `oidc.sync_sso_users`) commits after this call.
    """
    user = await session.get(AppUser, user_id)
    if user is not None:
        # Remove ALL sessions first and explicitly. Two pitfalls:
        # 1. `list_sessions` hides revoked/expired sessions whose foreign key still
        #    points at the user -- after a logout, the delete would then fail on
        #    user_session_user_id_fkey.
        # 2. No relationship is defined between AppUser and UserSession, so the ORM
        #    doesn't know about the dependency and would pick the order freely. A
        #    direct DELETE runs immediately and is thus guaranteed to run first.
        await session.execute(sa_delete(UserSession).where(UserSession.user_id == user_id))
        await session.delete(user)


async def delete_by_tenant(session: AsyncSession, tenant_id: int) -> int:
    """Delete all accounts bound to this tenant VIA SSO (together with their sessions) --
    part of the hard tenant delete cascade (`admin_tenants.delete_tenant`): the FK
    `app_user.tenant_id` is `ON DELETE SET NULL`, so it would not take these accounts
    down with the tenant delete, but would instead turn them into orphan accounts that
    look instance-wide. Sessions first, for the same reason as in `delete()`: no ORM
    relationship between `AppUser` and `UserSession`, a direct DELETE enforces the order.

    Carry-forward fix (whole-branch review, analogous to `delete_user`'s
    `user_token_repo.delete_created_by` call): `user_token.created_by` has NO
    `ON DELETE` -- a deleted creator account must not take down a still-valid token of
    ANOTHER user with it. If one of the SSO admins deleted here was the creator of a
    token (e.g. an invitation they sent, or a reset link), the DELETE below used to fail
    with an `IntegrityError` -- this path was the ONLY one of the two delete cascades
    that did not know about this cleanup step. The accounts' OWN tokens
    (`app_user_id`) still cascade automatically (`ON DELETE CASCADE`), only the
    `created_by` side needs to be cleared explicitly here.

    Returns the number of deleted accounts (for the audit details of the calling route).
    """
    res = await session.execute(
        select(AppUser.id).where(AppUser.is_sso.is_(True), AppUser.tenant_id == tenant_id)
    )
    ids = list(res.scalars().all())
    if ids:
        await session.execute(sa_delete(UserToken).where(UserToken.created_by.in_(ids)))
        await session.execute(sa_delete(UserSession).where(UserSession.user_id.in_(ids)))
        await session.execute(sa_delete(AppUser).where(AppUser.id.in_(ids)))
        await session.commit()
    return len(ids)


# ---- Sessions (refresh token rotation) ------------------------------------- #
async def create_session(
    session: AsyncSession,
    *,
    user_id: int,
    jti: str,
    token_hash: str,
    expires_at: dt.datetime,
    user_agent: str | None,
    ip: str | None,
    active_tenant_id: int | None = None,
) -> UserSession:
    us = UserSession(
        user_id=user_id,
        refresh_jti=jti,
        token_hash=token_hash,
        expires_at=expires_at,
        user_agent=user_agent,
        ip_address=ip,
        active_tenant_id=active_tenant_id,
    )
    session.add(us)
    await session.commit()
    await session.refresh(us)
    return us


async def get_session_by_jti(session: AsyncSession, jti: str) -> UserSession | None:
    res = await session.execute(select(UserSession).where(UserSession.refresh_jti == jti))
    return res.scalar_one_or_none()


async def list_sessions(session: AsyncSession, user_id: int) -> list[UserSession]:
    now = dt.datetime.now(dt.UTC)
    res = await session.execute(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked.is_(False),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_used_at.desc())
    )
    return list(res.scalars().all())


async def revoke_others(session: AsyncSession, user_id: int, keep_jti: str | None) -> int:
    """Log out all active sessions of the user except the current one."""
    count = 0
    for us in await list_sessions(session, user_id):
        if us.refresh_jti != keep_jti:
            us.revoked = True
            count += 1
    await session.commit()
    return count


async def prune_sessions(session: AsyncSession, user_id: int) -> None:
    """Remove expired/logged-out session records of the user (cleanup)."""
    now = dt.datetime.now(dt.UTC)
    res = await session.execute(
        select(UserSession).where(
            UserSession.user_id == user_id,
            or_(UserSession.revoked.is_(True), UserSession.expires_at <= now),
        )
    )
    for us in res.scalars().all():
        await session.delete(us)
    await session.commit()


async def delete_session_by_jti(session: AsyncSession, jti: str) -> None:
    """Remove the session entirely (logout, inactivity) instead of just revoking it."""
    await session.execute(sa_delete(UserSession).where(UserSession.refresh_jti == jti))
    await session.commit()


async def revoke_session(session: AsyncSession, jti: str) -> None:
    us = await get_session_by_jti(session, jti)
    if us:
        us.revoked = True
        await session.commit()


async def bump_token_generation(session: AsyncSession, user_id: int) -> None:
    """Invalidate all access tokens issued so far for this user (see AppUser.token_generation)."""
    await session.execute(
        update(AppUser)
        .where(AppUser.id == user_id)
        .values(token_generation=AppUser.token_generation + 1)
    )


async def revoke_all(session: AsyncSession, user_id: int) -> None:
    for us in await list_sessions(session, user_id):
        us.revoked = True
    await bump_token_generation(session, user_id)
    await session.commit()
