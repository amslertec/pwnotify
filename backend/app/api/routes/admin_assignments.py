"""Assignment API (access-model/superadmin phase, Task 4): which (active) tenants an
admin/auditor account may additionally manage/view beyond its home tenant --
SUPERADMIN-only on ALL routes (`SuperadminUser`, design §4/§6). Since context-gating v2
(matrix B), additionally DEFAULT-context-only (`SuperadminDefaultContextUser`,
`default_context_required`) -- the assignment console is provider-level and locked out
from within a customer context, exactly like the instance and tenant consoles.

**Core decision (deliberate deviation from the Task 4 brief):** the assignment kind
(`admin_tenant` vs. `auditor_tenant`) is NOT chosen by the caller via a dual list
(`{admin:[...], auditor:[...]}`), but is structurally derived from the target account's
ROLE -- `role=='admin'` -> `admin_tenant` (write capacity), otherwise (`auditor`) ->
`auditor_tenant` (read-only). A freely choosable grant type would have allowed a
`role=='admin'` account to receive ONLY an `auditor_tenant` assignment -- via the role
gate (`require_admin`) it would still have been allowed to act with WRITE access there,
even though the assignment itself was only meant to grant read access (the same error
class that `admin_users.create_local`, Task 3, already closes for account creation --
this route closes it for the SUBSEQUENT assignment).

A superadmin target account is NEVER assignable (it already sees all active tenants,
`tenant_repo.allowed_tenant_ids`) -- `PUT` hard-rejects that, `GET` defensively returns
an empty list instead of an error (pure read access, nothing that would need reconciling).

**Cross-grant lock (Task 2, the crown jewel of this route):** `set_assignments`
additionally checks `tenant_repo.is_provider_account(session, target)` -- a customer-homed
account (home tenant is NOT the default tenant, or `tenant_id is None`) may ONLY ever be
granted access to its OWN home tenant, never to a foreign one. What is deliberately
checked is the HOME tenant (`AppUser.tenant_id`), NOT the role: the role (`admin`/
`auditor`) only says which CAPACITY an assignment grants (write vs. read, see
`_grant_kind` above) -- it says nothing about whether the account is a provider account
or a customer account in the first place. A customer-homed `admin` and a customer-homed
`auditor` are equally cross-grant-locked; only the home tenant decides, not the role. The
default tenant is the deliberate exception: ONLY its (provider) accounts may the
superadmin grant access to any number of further active tenants -- that is the actual
purpose of this route (the IT service provider looks after several customers). Every
other account is structurally un-cross-grantable, even by the superadmin -- RLS and
`tenant_repo.is_allowed` remain the backstop layer, this lock is the API-side enforcement,
BEFORE any assignment row is written at all.

**Bulk assignment (`PUT /bulk`, Task 2 of the console+groups+invite phase):**
`bulk_assign` applies the cross-grant lock to EVERY account in the batch via EXACTLY the
same code path as `set_assignments` (provider check, `requested <= allowed`) --
deliberately NOT duplicated/rewritten. A security invariant that is checked in only ONE
place cannot drift apart via two slightly different implementations; a second, "similar"
lock check would be exactly the kind of duplicate that a future change forgets to apply
in the other place.

Two DIFFERENT error classes, deliberately handled asymmetrically:
- A cross-grant-locked, an unknown (`user_not_found`), or a superadmin target
  (`cannot_assign_superadmin`) is a PER-ACCOUNT policy decision -- the batch typically
  contains many accounts, and a single locked/invalid account should not prevent the
  other, legitimate reconciles. These accounts are skipped (`skipped`, see
  `schemas/assignment.py`), NOTHING is written for them, the rest of the batch proceeds
  normally.
- An unknown/inactive `tenant_id` in `body.tenant_ids`, by contrast, is a CALLER error
  (the same error case as `set_assignments`'s `tenant_not_active`) -- it affects the
  whole request, not a single account, and is therefore hard-checked UP FRONT (before any
  iteration over `user_ids`): the entire request fails BEFORE any row for any account has
  been written. Anything else would produce a partial success across the batch, dependent
  on a simple typo in `tenant_ids`.

`add_grant(..., source="manual")`: a bulk assignment is an explicit admin action (just
like the single assignment via `set_assignments`) -- `"manual"` ensures that a future
assignment-group reconcile (`source="group"`) respects this row and does not
overwrite/remove it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...models.user import AppUser
from ...repositories import tenant_repo, user_repo
from ...repositories.tenant_repo import _grant_kind
from ...schemas.assignment import (
    AssignmentOut,
    AssignmentUpdate,
    BulkAssignmentResult,
    BulkAssignmentUpdate,
    SkippedUser,
)
from ...services import audit
from ..deps import SessionDep, SuperadminDefaultContextUser

router = APIRouter(prefix="/admin/assignments", tags=["admin-assignments"])


async def _cross_grant_lock_allows(
    session: AsyncSession, target: AppUser, requested: set[int]
) -> bool:
    """THE one place where the cross-grant lock is evaluated -- called by both
    `set_assignments` AND `bulk_assign` (see module docstring "Bulk assignment"), so the
    security invariant structurally CANNOT drift apart (no two parallel copies of the same
    check, where a change in one place could be forgotten in the other).

    Provider account: unrestricted (`True`). Customer-homed account (or `tenant_id is
    None`): only allowed if `requested` is a subset of the account's own home tenant
    (empty set if no home tenant)."""
    if await tenant_repo.is_provider_account(session, target):
        return True
    allowed = {target.tenant_id} if target.tenant_id is not None else set()
    return requested <= allowed


async def _would_lock_out_last_tenant_admin(session: AsyncSession, tid: int, kind: str) -> bool:
    """L-01: THE one place where the lockout guard for a grant revocation is evaluated --
    called by both `set_assignments` AND `bulk_assign`, so the invariant structurally
    cannot drift apart (analogous to `_cross_grant_lock_allows`).

    A revocation only locks out a customer if ALL three hold:
    - It is an ADMIN grant (`kind == "admin"`): only write capacity can be lost; an
      `auditor_tenant` revocation can never cause a lockout.
    - The tenant is ACTIVE: a deactivated customer doesn't need a write admin, its
      stale assignments must remain cleanable (which is why `set_role`/`delete_user` also
      only checks via `admin_tenants`, which is active-joined -- same semantics here).
    - The customer would have no admin left afterwards: `count_tenant_admins` still counts
      the target (its grant is still intact at this point), so `<= 1` means exactly "it is
      the last one".

    The superadmin remains the rescue path -- this guard only closes the inconsistency
    relative to A4."""
    if kind != "admin":
        return False
    tenant = await tenant_repo.get(session, tid)
    if tenant is None or not tenant.is_active:
        return False
    return await user_repo.count_tenant_admins(session, tid) <= 1


@router.get("/{user_id}", response_model=AssignmentOut)
async def get_assignments(
    _: SuperadminDefaultContextUser, user_id: int, session: SessionDep
) -> AssignmentOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    if target.role == "superadmin":
        # Instance-wide, no assignment row is relevant -- nothing to display, no error.
        return AssignmentOut(role=target.role, tenant_ids=[])
    kind = _grant_kind(target.role)
    ids = await tenant_repo.list_grant_tenant_ids(session, user_id, kind)
    return AssignmentOut(role=target.role, tenant_ids=sorted(ids))


@router.put("/bulk", response_model=BulkAssignmentResult)
async def bulk_assign(
    request: Request,
    admin: SuperadminDefaultContextUser,
    body: BulkAssignmentUpdate,
    session: SessionDep,
) -> BulkAssignmentResult:
    """Reconciles `body.tenant_ids` against EVERY account in `body.user_ids` -- per account
    EXACTLY the same logic as `set_assignments` (see module docstring "Bulk assignment"
    above for the skip-instead-of-fail vs. hard-fail distinction).

    **Route ordering (security-relevant):** MUST be registered before `set_assignments`
    (`PUT /{user_id}`) -- otherwise Starlette tries to parse the literal path segment
    `"bulk"` as `{user_id}: int` and returns 422 instead of reaching this route."""
    # Caller errors are checked first and COMPLETELY up front (see module docstring): an
    # unknown/inactive tenant id rejects the ENTIRE request before even a single account of
    # the batch is touched -- no partial writes across the batch.
    #
    # ONLY for `action in {"add", "set"}` -- this check applies exclusively to tenants that
    # could actually be ADDED. `action="remove"` only ever removes existing assignments; an
    # assignment to a customer that was DEACTIVATED in the meantime must remain removable
    # (exactly the case a "customer deactivated" without prior cleanup of assignments
    # produces) -- otherwise this check would achieve the exact opposite of its purpose and
    # block the removal of a stale assignment.
    if body.action in ("add", "set"):
        for tid in set(body.tenant_ids):
            tenant = await tenant_repo.get(session, tid)
            if tenant is None or not tenant.is_active:
                raise ConflictError(
                    "Nur aktive Mandanten können zugewiesen werden.", code="tenant_not_active"
                )

    tenant_ids = set(body.tenant_ids)
    updated: list[int] = []
    skipped: list[SkippedUser] = []

    for user_id in body.user_ids:
        target = await user_repo.get(session, user_id)
        if target is None:
            skipped.append(SkippedUser(user_id=user_id, reason="user_not_found"))
            continue
        if target.role == "superadmin":
            skipped.append(SkippedUser(user_id=user_id, reason="cannot_assign_superadmin"))
            continue
        kind = _grant_kind(target.role)
        existing = set(await tenant_repo.list_grant_tenant_ids(session, user_id, kind))
        if body.action == "add":
            requested = existing | tenant_ids
        elif body.action == "remove":
            requested = existing - tenant_ids
        else:
            requested = set(tenant_ids)

        if not await _cross_grant_lock_allows(session, target, requested):
            # `_cross_grant_lock_allows` is the SAME check as in `set_assignments` -- see
            # module docstring, ONE code path for the invariant. Skip instead of fail: only
            # THIS account remains untouched, the rest of the batch continues.
            skipped.append(SkippedUser(user_id=user_id, reason="customer_account_not_grantable"))
            continue

        to_add = requested - existing
        to_remove = existing - requested
        for tid in sorted(to_add):
            await tenant_repo.add_grant(
                session, user_id=user_id, tenant_id=tid, kind=kind, source="manual"
            )
            await audit.record(
                session,
                action=audit.TENANT_ASSIGNED,
                actor=admin,
                target=target.username,
                request=request,
                detail={"tenant_id": tid, "kind": kind},
                # L-05: attribute to the affected tenant (see `set_assignments`), otherwise the
                # owner-session default_factory stamps NULL and the customer never sees it.
                tenant_id=tid,
            )
        for tid in sorted(to_remove):
            # L-01: same last-tenant-admin guard as `set_assignments`. Hard-fails the request
            # rather than skipping the account -- a last-admin revoke is a lockout, not a
            # per-account policy skip like the cross-grant lock above.
            if await _would_lock_out_last_tenant_admin(session, tid, kind):
                raise ConflictError(
                    "Der letzte Admin dieses Kunden kann nicht entzogen werden.",
                    code="last_tenant_admin",
                )
            await tenant_repo.remove_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
            await audit.record(
                session,
                action=audit.TENANT_UNASSIGNED,
                actor=admin,
                target=target.username,
                request=request,
                detail={"tenant_id": tid, "kind": kind},
                tenant_id=tid,  # L-05: attribute to the affected tenant (see to_add above).
            )
        updated.append(user_id)

    await session.commit()
    return BulkAssignmentResult(updated=updated, skipped=skipped)


@router.put("/{user_id}", response_model=AssignmentOut)
async def set_assignments(
    request: Request,
    admin: SuperadminDefaultContextUser,
    user_id: int,
    body: AssignmentUpdate,
    session: SessionDep,
) -> AssignmentOut:
    """Reconciles the assignments of `user_id` to exactly `body.tenant_ids` -- diffed
    against the current state (`tenant_repo.list_grant_tenant_ids`), add/remove delta,
    each change audited individually (design: per-tenant traceability, not just
    "changed")."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    if target.role == "superadmin":
        raise ConflictError(
            "Superadmins sehen bereits alle Mandanten -- keine Zuweisung nötig.",
            code="cannot_assign_superadmin",
        )
    kind = _grant_kind(target.role)
    requested = set(body.tenant_ids)

    if not await _cross_grant_lock_allows(session, target, requested):
        # Customer-homed account (see module docstring "Cross-grant lock"): the only
        # allowed assignment is its own home tenant -- any foreign id in `requested`
        # rejects the ENTIRE request before any assignment row is written.
        # `_cross_grant_lock_allows` is the same check that `bulk_assign` calls per account
        # (see module docstring "Bulk assignment") -- ONE code path for the invariant.
        raise ForbiddenError(
            "Kunden-Konten können nicht auf fremde Mandanten berechtigt werden.",
            code="customer_account_not_grantable",
        )

    for tid in requested:
        tenant = await tenant_repo.get(session, tid)
        if tenant is None or not tenant.is_active:
            raise ConflictError(
                "Nur aktive Mandanten können zugewiesen werden.", code="tenant_not_active"
            )

    existing = set(await tenant_repo.list_grant_tenant_ids(session, user_id, kind))
    to_add = requested - existing
    to_remove = existing - requested

    for tid in sorted(to_add):
        await tenant_repo.add_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
        await audit.record(
            session,
            action=audit.TENANT_ASSIGNED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"tenant_id": tid, "kind": kind},
            # L-05: attribute to the affected tenant so the customer sees the grant in its own
            # (tenant-scoped) log. On this owner session the ContextVar default_factory would
            # otherwise stamp NULL -- only superadmin-visible.
            tenant_id=tid,
        )
    for tid in sorted(to_remove):
        if await _would_lock_out_last_tenant_admin(session, tid, kind):
            raise ConflictError(
                "Der letzte Admin dieses Kunden kann nicht entzogen werden.",
                code="last_tenant_admin",
            )
        await tenant_repo.remove_grant(session, user_id=user_id, tenant_id=tid, kind=kind)
        await audit.record(
            session,
            action=audit.TENANT_UNASSIGNED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"tenant_id": tid, "kind": kind},
            tenant_id=tid,  # L-05: attribute to the affected tenant (see to_add above).
        )
    await session.commit()

    ids = await tenant_repo.list_grant_tenant_ids(session, user_id, kind)
    return AssignmentOut(role=target.role, tenant_ids=sorted(ids))
