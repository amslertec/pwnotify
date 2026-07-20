"""Benutzerverwaltung: lokale Konten (CRUD) + SSO-Konten (aus Entra-Gruppe)."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ...core.config import get_settings
from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...core.security import WEAK_PASSWORD_MESSAGE, hash_password, password_meets_policy
from ...db.tenant_context import tenant_scoped_session
from ...models._base import utcnow
from ...models.user import AppUser
from ...repositories import tenant_repo, user_repo, user_token_repo
from ...schemas.auth import (
    AdminUserCreate,
    AdminUserOut,
    RoleUpdate,
    SuperadminCreate,
    SuperadminToggle,
)
from ...schemas.common import Message
from ...services import audit, user_token
from ...services.settings_service import SettingsService
from ..deps import (
    ActiveTenantClaim,
    AdminUser,
    CurrentUser,
    SessionDep,
    SuperadminDefaultContextUser,
    _resolve_authorized_tenant,
    default_tenant_id,
    is_superadmin,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


# --------------------------------------------------------------------------- #
# Avatar path (Task B) -- own, minimal copy of `auth.py`'s `_avatar_path`
# (there `_avatar_dir()` + `_avatar_path(user_id)`, line 89/95), NOT imported from
# there: `auth.py` is read-only (reference) for this task, and an import would
# also needlessly couple this to its upload/2FA import tree. `get_settings()` is
# read PER CALL on purpose (not cached module-wide once like `auth.py`'s `_settings`)
# so that tests using `PWNOTIFY_DATA_DIR` + `get_settings.cache_clear()`
# (pattern from `test_branding_tenant_scope.py`) still take effect after this module import.
# No `mkdir` here (unlike `auth.py`'s `_avatar_dir`): this route only reads,
# never writes -- a non-existent directory is simply "no avatar".
def _avatar_path(user_id: int) -> Path:
    return Path(get_settings().data_dir) / "avatars" / f"{user_id}.png"


def _avatar_mtime(user_id: int) -> int | None:
    """mtime of the profile picture as a cache buster, or `None` if no picture exists or
    `data_dir` isn't readable. Any `OSError` (missing file, unreachable `/data`
    -- e.g. a fresh deploy before the volume mount or CI without `/data`) -> "no avatar", so
    user serialization never 500s on filesystem state (`Path.exists()` would let an
    `EACCES` propagate instead of treating it as "no")."""
    try:
        return int(_avatar_path(user_id).stat().st_mtime)
    except OSError:
        return None


def _admin_user_out(user: AppUser) -> AdminUserOut:
    """`AdminUserOut.model_validate(..., from_attributes=True)` + the file-based
    avatar fields (Task B) -- `from_attributes` doesn't fill those, `app_user` has no
    corresponding columns. `avatar_version` is the mtime as a cache buster, exactly the way
    `auth.py`'s `UserOut` construction does it for the own profile picture."""
    out = AdminUserOut.model_validate(user, from_attributes=True)
    if user.id is not None:
        mtime = _avatar_mtime(user.id)
        if mtime is not None:
            out.has_avatar = True
            out.avatar_version = mtime
    return out


@router.get("")
async def list_users(
    user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> dict[str, list[AdminUserOut]]:
    """Scoped account list for the Access page (Access rescope, security fix).

    **The security fix:** previously a superadmin, via `user_repo.list_all(session)`,
    ALWAYS saw the full, instance-wide account list -- regardless of the active tenant.
    When switching between customers, the Access page therefore showed the same global
    list every time instead of switching along. Now the confirmed model applies to
    EVERY caller (superadmin included): the Access page shows exclusively accounts whose
    HOME (`app_user.tenant_id`) is the ACTIVE tenant.

    **Resolving the active tenant:** the raw `active_tenant` claim (`ActiveTenantClaim`,
    unauthorized, see `deps.py`) if present, otherwise the default tenant
    (`deps.default_tenant_id`) -- the same fallback rule as on login/tenant switch.

    **Authorization:** the resolved tenant is ALWAYS checked before anything is returned.
    A superadmin may see any (active) tenant -- no additional check needed.
    Every other caller must pass `tenant_repo.is_allowed(session, user, tid)`; otherwise
    default-deny (empty lists) -- this prevents a local admin from listing a tenant they
    don't actually hold via a forged/stale `active_tenant` claim.

    Result per role:
    - **Superadmin** (`not is_sso and role=='superadmin'`): home accounts of the active
      tenant (local + SSO). Plus their own `superadmins` list (instance-wide, ALL
      superadmins) -- but ONLY if the active tenant is the DEFAULT tenant (provider
      context); in a customer context the `superadmins` key is entirely absent, even
      for the superadmin. Provider staff (homed at the default tenant) therefore only
      appear in the default view, not in any customer view -- cross-tenant assignments
      run through `/admin/assignments`, not through this route.
    - **Admin** (`role=='admin'`, LOCAL OR SSO): home accounts ONLY of the active tenant
      (local + SSO), provided they hold that tenant (see authorization above). An SSO
      admin holds their home tenant by core invariant (`admin_tenants` = `admin_tenant`
      grants merged with the SSO home for the admin role, design §2) plus any assigned
      customers -- they manage their customer's Access page exactly like a local admin.
      Never superadmins, never a `superadmins` key.
    - **Everything else** (auditor, unknown state): default-deny -> empty lists. The
      `/access` page is admin-only in the frontend, but this gate applies here
      independently of that as well.
    """
    if user.role not in ("admin", "superadmin"):
        return {"local": [], "sso": []}

    # Use the shared predicate (L6): `is_superadmin()` also excludes SSO accounts
    # (`not is_sso and role == "superadmin"`). A raw role compare would let an SSO account that
    # somehow held role=="superadmin" skip `is_allowed` below while `tid` comes from the
    # UNAUTHORIZED active-tenant claim -- a cross-tenant read one line from being reachable.
    is_superadmin_caller = is_superadmin(user)
    tid = active_tenant if active_tenant is not None else await default_tenant_id(session)

    if not is_superadmin_caller and not await tenant_repo.is_allowed(session, user, tid):
        return {"local": [], "sso": []}

    local_rows = await user_repo.list_local_homed_in_tenant(session, tid)
    sso_rows = await user_repo.list_sso_in_tenants(session, {tid})
    out: dict[str, list[AdminUserOut]] = {
        "local": [_admin_user_out(u) for u in local_rows],
        "sso": [_admin_user_out(u) for u in sso_rows],
    }

    if is_superadmin_caller and tid == await default_tenant_id(session):
        superadmin_rows = [u for u in await user_repo.list_all(session) if u.role == "superadmin"]
        out["superadmins"] = [_admin_user_out(u) for u in superadmin_rows]

    return out


@router.post("", response_model=AdminUserOut)
async def create_local(
    request: Request,
    admin: AdminUser,
    body: AdminUserCreate,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> AdminUserOut:
    """Creates a local account -- scoped by caller (Task 3).

    Superadmin: unrestricted, NO automatic assignment (the superadmin assigns tenants
    later, deliberately, Task 4). Every other admin caller (local admin or SSO admin):
    the new account is automatically assigned to the caller's ACTIVE tenant -- with the
    assignment kind matching the new role (`role=='admin'` -> `admin_tenant`,
    `role=='auditor'` -> `auditor_tenant`), so a `role=='admin'` account never has ONLY an
    `auditor_tenant` assignment (that would grant it write access via the role gate that
    the assignment itself doesn't confer).

    The `active_tenant` claim is NOT taken at face value (per `ActiveTenantClaim` it is
    unauthorized, meant for display only) -- instead it's additionally checked via
    `tenant_repo.is_allowed(..., write=True)`. If the claim is missing or there is no
    write membership, the request is clearly rejected instead of creating an invisible,
    unassigned account.

    **Setting the home tenant (context gating v2, Task 3):** previously the new account
    got an `admin_tenant`/`auditor_tenant` row, but NEVER a `tenant_id` (home) -- which
    left the cross-grant lock (Task 2, `tenant_repo.is_provider_account`,
    `admin_assignments.set_assignments`) without a basis: an account without a home counts
    there as a customer account with an EMPTY allowed set (`tenant_id is None` -> not a
    provider), so over-restrictive for an account created by a superadmin AND without a
    real customer home for an account created by a customer admin. Hence now explicit:
    - Non-superadmin caller (local/SSO admin for their active customer): home =
      `grant_tenant_id` (the same active tenant already checked via
      `is_allowed(..., write=True)`) -- the new account is thus customer-homed AND
      correspondingly assigned, so per Task 2 it's structurally not cross-grantable to a
      foreign tenant -- purely a customer-staff account.
    - Superadmin caller: home = the default tenant (`deps.default_tenant_id`) -- provider
      staff are default-homed, so an account created this way remains cross-grantable to
      any customer via the assignment API (Task 4/cross-grant lock Task 2).
      (A superadmin creating a *superadmin* account remains unchanged in
      `create_superadmin` -- instance-wide, no home needed.)

    **Invitation mode (Task 5, §7b):** `body.password` ABSENT switches to invitation mode --
    `body.username` is deliberately NOT taken from the caller here; instead the route
    assigns a guaranteed-unique, clearly not-loggable-in placeholder (`pending:<uuid4>`) +
    an unusable password hash (`hash_password(secrets.token_hex(32))`, no known plaintext
    password exists for it) + `is_active=False`. This avoids schema churn (no nullable
    `username`); the accept endpoint (`api/routes/public_tokens.py`) overwrites the
    placeholder on redemption with the real name, uniqueness-checked only there. Home
    tenant + assignment run exactly as above (unchanged by caller role) -- invitation mode
    only changes WHERE the account identity comes from, never the scoping rules.
    """
    raw_password = body.password
    is_invite = raw_password is None

    username: str
    password_hash: str
    if raw_password is None:
        if not body.email:
            raise ForbiddenError(
                "Für eine Einladung ist eine E-Mail-Adresse erforderlich.",
                code="email_required",
            )
        username = f"pending:{uuid.uuid4().hex}"
        password_hash = hash_password(secrets.token_hex(32))  # never redeemable
    else:
        if not body.username:
            raise ForbiddenError("Benutzername erforderlich.", code="username_required")
        existing = await user_repo.get_by_username(session, body.username)
        if existing is not None:
            raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
        username = body.username
        # Full server-side password policy (Security Phase 5, Task 2) -- pydantic's
        # `min_length=10` on `AdminUserCreate.password` is only a floor. Direct mode only --
        # an invite (`raw_password is None`, handled above) never sets a real password here.
        if not password_meets_policy(raw_password):
            raise ForbiddenError(WEAK_PASSWORD_MESSAGE, code="password_policy")
        password_hash = hash_password(raw_password)

    is_superadmin_caller = not admin.is_sso and admin.role == "superadmin"
    grant_tenant_id: int | None = None
    if not is_superadmin_caller:
        if active_tenant is None or not await tenant_repo.is_allowed(
            session, admin, active_tenant, write=True
        ):
            raise ForbiddenError(
                "Kein aktiver Mandant mit Verwaltungsrechten.", code="tenant_required"
            )
        grant_tenant_id = active_tenant

    home_tenant_id = (
        grant_tenant_id if not is_superadmin_caller else await default_tenant_id(session)
    )

    user = await user_repo.create(
        session,
        username=username,
        password_hash=password_hash,
        display_name=body.display_name,
        role=body.role,
        is_sso=False,
        tenant_id=home_tenant_id,
    )
    assert user.id is not None  # just committed, so it has an id

    if is_invite:
        # Invitation: pending -- account exists but is unusable until acceptance
        # (`public_tokens.accept_token`). Email is set here (reset-trigger anchor §7c),
        # not on the `create()` call above (which stays unchanged for the direct path).
        user.email = body.email
        user.is_active = False
        user.updated_at = utcnow()
        await session.commit()
        await session.refresh(user)

    if grant_tenant_id is not None:
        kind = "admin" if body.role == "admin" else "auditor"
        await tenant_repo.add_grant(session, user_id=user.id, tenant_id=grant_tenant_id, kind=kind)

    detail: dict[str, object] = {"role": body.role, "sso": False, "home_tenant_id": home_tenant_id}
    if grant_tenant_id is not None:
        detail["granted_tenant_id"] = grant_tenant_id
    if is_invite:
        detail["email"] = body.email

    await audit.record(
        session,
        action=audit.USER_INVITED if is_invite else audit.USER_CREATED,
        actor=admin,
        target=username,
        request=request,
        detail=detail,
        # Owner-session route (Task 7/M11): `home_tenant_id` is the new account's own home,
        # already resolved above -- always a real tenant here, never NULL.
        tenant_id=home_tenant_id,
    )
    await session.commit()

    if is_invite:
        assert admin.id is not None
        await user_token.issue_invite(session, user=user, created_by=admin.id)

    return _admin_user_out(user)


@router.post("/{user_id}/reset", response_model=Message)
async def send_reset(
    request: Request, admin: AdminUser, user_id: int, session: SessionDep
) -> Message:
    """Triggers a password-reset link for an EXISTING local account (Task 5, §7c).

    **Authorization:** the same subset rule as `set_role`/`delete_user` (see there for
    the detailed rationale) -- a superadmin caller skips it (full access); every other
    caller needs the target's ENTIRE tenant membership within their own managed tenants
    (subset, not intersection rule). A target without any tenant membership is accessible
    ONLY to a superadmin.

    **Business guards after that** (order deliberate: authorize first, then validate):
    an SSO target is rejected (`sso_no_reset` -- its password lives in Entra, a local
    reset link would be ineffective/misleading); a target without an email on file is
    also rejected (`email_required` -- the admin must set one in the edit dialog first,
    there's no address for the link to go to).

    Minting + sending run through `services.user_token.issue_reset` (which idempotently
    invalidates older, still-valid reset tokens of the same account, see there)."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")

    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )

    if target.is_sso:
        raise ForbiddenError(
            "SSO-Konten setzen ihr Passwort über Microsoft Entra zurück.",
            code="sso_no_reset",
        )
    if target.email is None:
        raise ForbiddenError(
            "Für dieses Konto ist keine E-Mail-Adresse hinterlegt.", code="email_required"
        )

    assert admin.id is not None
    await user_token.issue_reset(session, user=target, created_by=admin.id)

    await audit.record(
        session,
        action=audit.PASSWORD_RESET_SENT,
        actor=admin,
        target=target.username,
        request=request,
        detail={"target_user_id": user_id},
        # Owner-session route (Task 7/M11): attribute to the target's own home tenant, the
        # unambiguous anchor -- NULL stays NULL for a homeless (provider) target. A superadmin
        # target's `tenant_id` is only a branding anchor from its invite (Default-Tenant),
        # never a real home -- stamping it would leak a provider-level event into that
        # tenant's audit view (Review-Fix, Task 7/M11).
        tenant_id=(target.tenant_id if target.role != "superadmin" else None),
    )
    await session.commit()
    return Message(message="Link zum Zurücksetzen des Passworts wurde versendet.")


@router.post("/{user_id}/role", response_model=AdminUserOut)
async def set_role(
    request: Request, admin: AdminUser, user_id: int, body: RoleUpdate, session: SessionDep
) -> AdminUserOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # A superadmin target NEVER goes through this path (Task 4, access-model/superadmin
    # phase): this gate is `AdminUser`, not `SuperadminUser` -- otherwise a PLAIN admin
    # could demote the last superadmin to an ordinary admin unnoticed via
    # `RoleUpdate.role='admin'` (allowed by the schema), without the last-superadmin
    # protection (which only lives in `set_superadmin`) ever kicking in. The switch
    # to/from superadmin runs exclusively through `set_superadmin` (superadmin-only).
    if target.role == "superadmin":
        raise ForbiddenError(
            "Superadmin-Rollenwechsel nur über die Superadmin-Verwaltung möglich.",
            code="superadmin_required",
        )
    # Cross-tenant fix (security review, whole-branch review access-model/superadmin
    # phase): Task 3 scoped `list_users`/`create_local`, but `set_role` remained gated
    # only via `AdminUser` (any admin/superadmin of ANY tenant) and resolved `target`
    # without RLS on `app_user` (instance-wide) -- so a local admin of tenant A could
    # change the role of an account that belongs EXCLUSIVELY to tenant B (IDs are
    # sequentially enumerable). A superadmin caller skips this check (full access,
    # already constrained by the guard above). For every other caller, the target's
    # ENTIRE tenant membership must lie within the tenants MANAGED by the caller
    # (subset rule, not mere intersection) -- `app_user` is instance-wide, so an account
    # can additionally belong to a tenant the caller doesn't hold; a pure intersection
    # test would still let the role change through and thereby unintentionally hit the
    # foreign tenant too. A target with no tenant membership at all (empty set) may
    # ONLY be touched by a superadmin -- hence `not target_scope` as its own rejection
    # reason.
    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Don't demote the last administrator -- otherwise no one could manage the instance
    # anymore. This also covers self-demotion when you're the only admin.
    if (
        target.role == "admin"
        and body.role != "admin"
        and await user_repo.count_admins(session) <= 1
    ):
        raise ConflictError(
            "Der letzte Administrator kann nicht herabgestuft werden.",
            code="cannot_demote_last_admin",
        )
    # Per-tenant lockout protection (A4): the instance-wide `count_admins` guard above only
    # ensures that an admin remains SOMEWHERE -- not that EVERY customer keeps its last
    # (write) admin. A local admin of customer A could otherwise strip A's last admin
    # while an admin of ANOTHER tenant keeps the instance-wide count > 1; only the
    # provider superadmin could then still rescue it. Hence: when an admin target is
    # demoted, every tenant it currently administers (`admin_tenants` = `admin_tenant`
    # grants merged with the SSO home) must retain >= 1 admin afterwards.
    # `count_tenant_admins` still counts the target itself (grant/home exist at this
    # point), so `<= 1` means exactly "the target is the last one". Only relevant if the
    # target is REALLY an admin -- an auditor target has no `admin_tenant` grants,
    # `admin_tenants` is empty, so the loop doesn't run.
    if target.role == "admin" and body.role != "admin":
        for tid in await tenant_repo.admin_tenants(session, target):
            if await user_repo.count_tenant_admins(session, tid) <= 1:
                raise ConflictError(
                    "Der letzte Admin dieses Kunden kann nicht herabgestuft werden.",
                    code="last_tenant_admin",
                )
    vorher = target.role
    target.role = body.role
    # Grant migration (Task 4, H8): keep the target's tenant grant rows in sync with its new
    # role so capability (read vs. write) never lags behind the role change. Without this, a
    # stale `auditor_tenant` grant from before an auditor->admin promotion (or a stale
    # `admin_tenant` grant after an admin->auditor demotion) would let the write gate
    # (`_resolve_authorized_tenant(..., write=True)`) mislabel the account (Minor-1, closed
    # here). SSO targets are excluded -- their grants are group-driven and reconciled on
    # login, not managed by this route. `add_grant`/`remove_grant` commit internally; this
    # runs BEFORE the final `session.commit()` below so no double-commit races the pending
    # `target.role` write -- both converge into the same already-committed state.
    if not target.is_sso and vorher != body.role and {vorher, body.role} <= {"admin", "auditor"}:
        assert target.id is not None  # persisted account: id is always set here
        old_kind = "admin" if vorher == "admin" else "auditor"
        new_kind = "admin" if body.role == "admin" else "auditor"
        for tid in await tenant_repo.list_grant_tenant_ids(session, target.id, old_kind):
            await tenant_repo.add_grant(session, user_id=target.id, tenant_id=tid, kind=new_kind)
            await tenant_repo.remove_grant(session, user_id=target.id, tenant_id=tid, kind=old_kind)
    await audit.record(
        session,
        action=audit.USER_ROLE_CHANGED,
        actor=admin,
        target=target.username,
        request=request,
        detail={"from": vorher, "to": body.role, "sso": target.is_sso},
        # Owner-session route (Task 7/M11): attribute to the target's own home tenant.
        tenant_id=target.tenant_id,
    )
    await session.commit()
    await session.refresh(target)
    return _admin_user_out(target)


@router.post("/superadmin", response_model=AdminUserOut)
async def create_superadmin(
    request: Request,
    admin: SuperadminDefaultContextUser,
    body: SuperadminCreate,
    session: SessionDep,
) -> AdminUserOut:
    """Creates a LOCAL superadmin -- superadmin-only (design §11.3: superadmin is
    ALWAYS a local account, never SSO). NO automatic assignment: the superadmin is
    instance-wide and needs no `admin_tenant`/`auditor_tenant` row (unlike `create_local`
    for ordinary admin/auditor accounts, Task 3).

    Since context gating v2 (matrix B) additionally only in the DEFAULT context
    (`SuperadminDefaultContextUser`, `default_context_required`): superadmin management
    is provider-level (design §4/§4-notes), just like the instance/tenant/assignment
    console -- locked out from a customer context.

    **Invitation mode (Task 10, parity with `create_local`'s invitation mode, Task 5, §7b):**
    `body.password` ABSENT switches to invitation mode -- exact same pattern as there
    (placeholder username `pending:<uuid4>`, unusable password hash, `is_active=False`), BUT
    WITHOUT `add_grant` (superadmin is instance-wide, no tenant assignment needed) and with
    `tenant_id = default_tenant_id(...)` as home -- NOT because the superadmin "belongs" to
    any tenant, but because sending the invitation (`user_token._send`) runs inside
    `tenant_scoped_session(user.tenant_id)` and thereby resolves branding; a homeless
    account (`tenant_id=None`, as in the direct path below -- deliberately unchanged there,
    no mail sending needed) would have no branding scope. The accept endpoint
    (`public_tokens.accept_token`) is ROLE-AGNOSTIC -- it never touches `target.role` --
    so a `pending` account created with `role='superadmin'` correctly activates as a
    superadmin, with no change needed to `public_tokens.py`/`user_token*.py`."""
    if body.is_sso:
        raise ConflictError(
            "Ein Superadmin muss ein lokales Konto sein.", code="superadmin_must_be_local"
        )

    raw_password = body.password
    is_invite = raw_password is None

    username: str
    password_hash: str
    if raw_password is None:
        if not body.email:
            raise ForbiddenError(
                "Für eine Einladung ist eine E-Mail-Adresse erforderlich.",
                code="email_required",
            )
        username = f"pending:{uuid.uuid4().hex}"
        password_hash = hash_password(secrets.token_hex(32))  # never redeemable
    else:
        if not body.username:
            raise ForbiddenError("Benutzername erforderlich.", code="username_required")
        existing = await user_repo.get_by_username(session, body.username)
        if existing is not None:
            raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
        username = body.username
        # Full server-side password policy (Security Phase 5, Task 2) -- pydantic's
        # `min_length=10` on `SuperadminCreate.password` is only a floor. Direct mode only --
        # an invite (`raw_password is None`, handled above) never sets a real password here.
        if not password_meets_policy(raw_password):
            raise ForbiddenError(WEAK_PASSWORD_MESSAGE, code="password_policy")
        password_hash = hash_password(raw_password)

    user = await user_repo.create(
        session,
        username=username,
        password_hash=password_hash,
        display_name=body.display_name,
        role="superadmin",
        is_sso=False,
        tenant_id=await default_tenant_id(session) if is_invite else None,
    )
    assert user.id is not None  # just committed, so it has an id

    if is_invite:
        # Invitation: pending -- account exists but is unusable until acceptance
        # (`public_tokens.accept_token`). Email is set here (like `create_local`'s
        # invitation path), not on the `create()` call above (which stays unchanged for
        # the direct path).
        user.email = body.email
        user.is_active = False
        user.updated_at = utcnow()
        await session.commit()
        await session.refresh(user)

    await audit.record(
        session,
        action=audit.USER_INVITED if is_invite else audit.SUPERADMIN_CREATED,
        actor=admin,
        target=username,
        request=request,
        detail={"role": "superadmin", "sso": False, "email": body.email} if is_invite else None,
    )
    await session.commit()

    if is_invite:
        assert admin.id is not None
        await user_token.issue_invite(session, user=user, created_by=admin.id)

    return _admin_user_out(user)


@router.post("/{user_id}/superadmin", response_model=AdminUserOut)
async def set_superadmin(
    request: Request,
    admin: SuperadminDefaultContextUser,
    user_id: int,
    body: SuperadminToggle,
    session: SessionDep,
) -> AdminUserOut:
    """Promotes/demotes to/from superadmin -- the ONLY path for that (`set_role` hard-rejects
    any role change of a superadmin target, see above). Superadmin-only.

    Since context gating v2 (matrix B) additionally only in the DEFAULT context
    (`SuperadminDefaultContextUser`, `default_context_required`): same provider-level
    rationale as `create_superadmin` above.

    Promoting: only a LOCAL target (`not is_sso`) may become superadmin (design §11.3,
    `code="superadmin_must_be_local"`) -- its previous `admin_tenant`/
    `auditor_tenant` assignments are cleared in the process (deliberate decision: the
    superadmin sees all active tenants anyway, orphaned assignment rows would be pure
    data clutter and would otherwise surprisingly come back to life on a future demotion).

    Demoting: the last ACTIVE superadmin may not be demoted (design §11.4,
    `code="cannot_demote_last_superadmin"`) -- otherwise no one could manage the instance
    anymore. The target falls back to `role="admin"` in that case (no finer role below
    superadmin is defined here)."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")

    if body.promote:
        if target.role == "superadmin":
            return _admin_user_out(target)
        if target.is_sso:
            raise ConflictError(
                "Nur lokale Konten können zu Superadmin befördert werden.",
                code="superadmin_must_be_local",
            )
        vorher = target.role
        target.role = "superadmin"
        assert target.id is not None  # bereits persistiert (kam aus user_repo.get)
        for existing_kind in ("admin", "auditor"):
            for tid in await tenant_repo.list_grant_tenant_ids(session, target.id, existing_kind):
                await tenant_repo.remove_grant(
                    session, user_id=target.id, tenant_id=tid, kind=existing_kind
                )
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": vorher, "to": "superadmin", "sso": target.is_sso},
        )
    else:
        if target.role != "superadmin":
            return _admin_user_out(target)
        if await user_repo.count_superadmins(session) <= 1:
            raise ConflictError(
                "Der letzte Superadmin kann nicht herabgestuft werden.",
                code="cannot_demote_last_superadmin",
            )
        target.role = "admin"
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": "superadmin", "to": "admin", "sso": target.is_sso},
        )

    await session.commit()
    await session.refresh(target)
    return _admin_user_out(target)


@router.delete("/{user_id}", response_model=Message)
async def delete_user(
    request: Request, user: AdminUser, user_id: int, session: SessionDep
) -> Message:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # Deletion is blocked when there is only a single user left.
    if await user_repo.count(session) <= 1:
        raise ConflictError("Der letzte Benutzer kann nicht gelöscht werden.", code="last_user")
    if target.id == user.id:
        raise ConflictError(
            "Sie können Ihr eigenes Konto nicht löschen.", code="cannot_delete_self"
        )
    # Superadmin target: NEVER deletable by a non-superadmin caller (Task 4,
    # access-model/superadmin phase -- security review fix). This gate is `AdminUser`,
    # not `SuperadminUser` -- without this check a PLAIN admin or an SSO admin could
    # remove every NON-last superadmin by deletion, repeatedly down to the last one
    # (the last-superadmin protection below only kicks in AT the last one). Analogous to
    # `set_role`'s protection for role changes of a superadmin target -- same error code.
    if target.role == "superadmin" and (user.is_sso or user.role != "superadmin"):
        raise ForbiddenError(
            "Superadmin-Löschung nur durch einen Superadmin möglich.",
            code="superadmin_required",
        )
    # Cross-tenant fix (security review, whole-branch review access-model/superadmin
    # phase) -- analogous to the scope check in `set_role` above: `delete_user` was only
    # gated via `AdminUser` and resolved `target` without RLS, so a local admin of tenant A
    # could delete a user that belongs ONLY to tenant B. A superadmin caller skips this
    # check (full access). For every other caller, the target's ENTIRE tenant membership
    # must lie within the tenants MANAGED by the caller (subset, not intersection rule --
    # otherwise deleting an account also assigned to B would unintentionally hit tenant B
    # too, since `app_user` is instance-wide). A target with no tenant membership at all
    # may ONLY be deleted by a superadmin.
    if user.is_sso or user.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, user)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Last-superadmin protection (design §11.4) -- analogous to the last-admin protection
    # above, but for the instance-wide role: without this check, someone could lock out
    # the last superadmin by deleting them.
    if target.role == "superadmin" and await user_repo.count_superadmins(session) <= 1:
        raise ConflictError(
            "Der letzte Superadmin kann nicht gelöscht werden.",
            code="cannot_delete_last_superadmin",
        )
    # Per-tenant lockout protection (A4) -- mirror of the `set_role` guard: `delete_user`
    # previously had NO admin-count guard at all (only last-user, self-deletion, last-
    # superadmin), so an admin could delete themselves as the last admin of a customer as
    # long as another tenant kept the instance-wide count > 1. Before deleting an admin
    # target: every tenant it administers must retain >= 1 admin afterwards (same
    # counting/edge conditions as in `set_role`; `count_tenant_admins` still includes the
    # still-existing target, so `<= 1` means "the target is the last one").
    if target.role == "admin":
        for tid in await tenant_repo.admin_tenants(session, target):
            if await user_repo.count_tenant_admins(session, tid) <= 1:
                raise ConflictError(
                    "Der letzte Admin dieses Kunden kann nicht gelöscht werden.",
                    code="last_tenant_admin",
                )
    # Atomicity (L-04/M-03): STAGE the audit entry, then the deletion, and commit ONCE at
    # the end -- both land in the same transaction. Previously the audit was committed HERE,
    # BEFORE the deletion below, so a failure in `delete_created_by`/`delete` left a phantom
    # `USER_DELETED` in the log for an account that still existed.
    await audit.record(
        session,
        action=audit.USER_DELETED,
        actor=user,
        target=target.username,
        request=request,
        detail={"role": target.role, "sso": target.is_sso},
        # Owner-session route (Task 7/M11): attribute to the target's own home tenant. A
        # superadmin target's `tenant_id` is only a branding anchor from its invite
        # (Default-Tenant), never a real home -- stamping it would leak a provider-level
        # event into that tenant's audit view (Review-Fix, Task 7/M11).
        tenant_id=(target.tenant_id if target.role != "superadmin" else None),
    )
    # Carry-forward fix from Task 1: `user_token.created_by` has NO `ON DELETE` (a
    # deleted creator account must not take down a still-valid token of ANOTHER user)
    # -- without this step BEFORE the actual deletion, it fails with an `IntegrityError`
    # as soon as `target` still has open, self-issued tokens (e.g. an invitation it sent
    # or a reset link). Mirrors the session deletion that `user_repo.delete` already
    # handles internally for the tokens of the DELETED account itself (cascaded via
    # `app_user_id`, not needed for that).
    await user_token_repo.delete_created_by(session, user_id)
    await user_repo.delete(session, user_id)
    await session.commit()
    return Message(message="Benutzer gelöscht.")


@router.post("/sso/sync", response_model=Message)
async def sync_sso(request: Request, user: AdminUser, session: SessionDep) -> Message:
    """Reconcile SSO users against each tenant's own Entra group configuration.

    Scope: a non-superadmin admin reconciles ONLY their own authorized active tenant. Instance-
    wide reconciliation (all active tenants) stays superadmin-exclusive. `app_user` is instance-
    wide (no RLS), so the write path (`oidc.sync_sso_users`) runs on the owner `session`; the
    per-tenant `oidc.*` settings are read via a tenant-scoped session inside the loop.
    """
    from ...services import oidc

    tid: int | None = None
    if is_superadmin(user):
        tenants = await tenant_repo.list_active(session)
    else:
        # WRITE gate (M1): `sync_sso_users` creates, re-activates, overwrites roles on and
        # DELETES SSO accounts -- a destructive write. A read-only `auditor_tenant` grant must
        # not be enough to trigger it (sister route `runs.trigger` gates the same way).
        tid = await _resolve_authorized_tenant(request, user, session, write=True)
        tenant = await tenant_repo.get(session, tid)
        tenants = [tenant] if tenant is not None else []

    configured = False
    synced = removed = 0
    blocked_count = 0
    for tenant in tenants:
        assert tenant.id is not None  # persisted row from the DB
        async with tenant_scoped_session(tenant.id) as tsession:
            settings = await SettingsService(tsession).get_all()
        if not settings.get("oidc.enabled") or not settings.get("oidc.admin_group_id"):
            continue
        configured = True
        stats = await oidc.sync_sso_users(session, settings, tenant_id=tenant.id)
        synced += stats["synced"]
        removed += stats["removed"]
        if stats.get("removal_blocked"):
            blocked_count += 1

    if not configured:
        raise ConflictError(
            "SSO ist nicht aktiviert oder keine Admin-Gruppe hinterlegt.", code="sso_not_configured"
        )
    message = f"{synced} SSO-Benutzer synchronisiert, {removed} entfernt."
    if blocked_count:
        # Report a COUNT, never tenant names -- no cross-tenant name disclosure.
        message += f" Entfernen für {blocked_count} Mandant(en) blockiert (Schutz vor Aussperrung)."
    # M-02: the SSO_SYNCED audit entry is written INSIDE `oidc.sync_sso_users` now (per synced
    # tenant, atomically with any USER_DELETED it stages), so the SCHEDULED runner path is
    # audited too -- not only this manual route. Writing it a second time here would double-log.
    await session.commit()
    return Message(message=message)


@router.get("/{user_id}/avatar")
async def get_user_avatar(
    request: Request, admin: AdminUser, user_id: int, session: SessionDep
) -> FileResponse:
    """Profile photo of ONE account for the Access page (Task B) -- counterpart to
    `auth.py`'s `GET /auth/me/avatar`, but admin-facing (arbitrary `user_id`, not just the
    caller). Gate is `AdminUser` (any admin/superadmin) PLUS the same subset-scope rule
    `set_role`/`delete_user`/`send_reset` already use (Task 6, M6 fix -- previously this
    route trusted `AdminUser` alone and served ANY account's cached photo, letting an admin
    of tenant A read a foreign account's picture, `user_id`s being sequential and
    enumerable). A local superadmin bypasses the check (full instance-wide access, same as
    the other routes); every other caller needs the target's ENTIRE tenant membership to be
    a subset of the caller's own managed tenants.

    Out-of-scope and non-existent both raise the SAME `NotFoundError("no_avatar")` --
    deliberately `NotFoundError`, not `ForbiddenError`, unlike the mutation routes above: a
    403-vs-404 split (or a 200-vs-404 split) here would itself be an existence oracle for
    `user_id`, and this route has no mutation to guard, only leakage to avoid.

    No Graph round-trip -- the file is already cached locally (SSO login cache or a
    self-upload), this route only reads.

    `Cache-Control: max-age=3600`: unlike `auth.py`'s `/me/avatar` (`no-cache` there, since a
    self-upload must be visible immediately) the URL here always carries `avatar_version` as
    a cache-busting query (`?v=...`, see `access.tsx`) -- a new version gets a new URL, so
    long caching of the old URL is safe and reduces load on the Access page for large
    account lists."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    if _avatar_mtime(user_id) is None:
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    return FileResponse(
        _avatar_path(user_id), media_type="image/png", headers={"Cache-Control": "max-age=3600"}
    )
