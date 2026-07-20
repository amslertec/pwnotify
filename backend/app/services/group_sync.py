"""SECURITY-CRITICAL (crown jewel of the increment): the proactive Entra group sync.

`sync_group` fetches the (transitive) members of an `AssignmentGroup` from Microsoft Graph,
brings the local snapshot (`assignment_group_member`) up to that state, and materializes
customer access from it -- BUT exclusively via the already security-reviewed
`assignment_group_repo.reconcile_group_grants`. This sync has NO grant write path of its
own.

WHY `reconcile_group_grants` is reused (instead of separate grant logic):
    The hard isolation invariant of the multi-tenant product lives in exactly ONE gated
    code location -- the `is_provider_account` gate (first line, fail-closed) of
    `reconcile_group_grants`. A customer-homed or `tenant_id is None` account therefore
    NEVER receives a group grant, even if it shows up (e.g. due to a misconfiguration) in
    a foreign team's snapshot. A second, forked grant path here would duplicate this
    guarantee and could drift from it -- hence: one materialization, one gate.

MATCH RULE (UPN -> local account): `user_repo.get_by_username(session, upn)`, with the UPN
    stored EXACTLY as in the snapshot (from Graph `userPrincipalName`). CASE-SENSITIVE --
    deliberately identical to the login path: the SSO user sync (`services/oidc.py`) also
    matches via `get_by_username` (exact, `AppUser.username == username`) and stores the
    username raw (no lowercasing). A case-insensitive match here would grant access
    differently than login does -- that inconsistency would itself be a bug. Unmatched
    members remain only in the snapshot; their access is created on first SSO login via
    the unchanged login reconcile. NO accounts are created here.

OLD/NEW reconcile set (union): before the snapshot reconcile, the match set of this
    group's OLD members is captured; after the reconcile, the NEW set -- what gets
    reconciled is the UNION. This way a member removed from the group in THIS run is also
    reconciled: its team set no longer includes this group, and its now-orphaned
    `source='group'` grant is revoked (a `source='manual'` grant remains).

SNAPSHOT AS SOURCE OF TRUTH (deliberate design, not a defect): an account's team set for
    this sync is derived from ALL local snapshots
    (`assignment_group_member_repo.groups_containing_upn`), after this group's reconcile.
    The login reconcile (from live Graph claims) remains the always-fresh primary path;
    this sync is the proactive supplement that acts between two logins of an account.

`transitiveMembers` (from Task 2): `graph.get_group_members` queries `transitiveMembers/
    microsoft.graph.user` -- nested groups are resolved, the OData cast restricts the
    result to real user accounts (no devices/service principals).

COMMIT: no own `session.commit()` -- the caller (Task 4 route) commits the transaction.
    The individually used `add_grant`/`remove_grant` calls in `reconcile_group_grants`
    already commit per row (same transaction semantics as the login path).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import GraphError, NotFoundError, PwNotifyError
from ..models._base import utcnow
from ..models.assignment_group import AssignmentGroup
from ..models.user import AppUser
from ..repositories import assignment_group_member_repo as member_repo
from ..repositories import assignment_group_repo, tenant_repo, user_repo
from . import audit
from .graph.client import GraphClient, GraphConfig


class GroupSyncError(PwNotifyError):
    """A group sync failed on an expected, upstream-caused error (group not found in
    Graph, missing permission, transport error). Typed and message-carrying so the route
    cleanly renders it as a `sync_failed` response instead of an unhandled 500 -- snapshot
    and grants remain unchanged."""

    status_code = 502
    code = "sync_failed"


async def _is_fully_deprovisioned(session: AsyncSession, account: AppUser) -> bool:
    """SECURITY-CRITICAL -- the fail-safe gate before deleting an account (`sync_group`).

    Deletion is the DANGEROUS branch; the default is KEEP. Returns True ONLY if ALL
    conditions hold -- a single unmet condition short-circuits to False (keep). Order:
    cheapest/most decisive first, fail-closed.

    1. `account.is_sso is True` -- a local account is NEVER deleted.
    2. `account.role != "superadmin"` -- a superadmin is NEVER deleted (a superadmin is
       `not is_sso and role=="superadmin"`, so already excluded by (1); made explicit here
       too, belt and suspenders).
    3. `is_provider_account` -- home is the default tenant (this also excludes `tenant_id
       is None` -- a customer-homed or homeless account is not a provider account and must
       NOT be deleted here).
    4. `not groups_containing_upn` -- no longer shows up in ANY team snapshot (empty set),
       evaluated AFTER this run's snapshot reconcile.
    5. Holds NO grant row in EITHER of the two grant tables. IMPORTANT: checked directly
       against the grant TABLES (`list_grant_tenant_ids`), NOT against
       `tenant_repo.admin_tenants`/`auditor_tenants` -- the latter fold an SSO account's
       home tenant into the set (`admin_tenants` adds `user.tenant_id` when `is_sso and
       role=="admin"`), which would make a provider account homed on the default tenant
       ALWAYS look "granted" and thus never deletable. The home-tenant membership is
       inherent to the provider account and must NOT count as a grant. The raw rows cover
       both `source='group'` (already revoked by the reconcile) and `source='manual'`
       (must REMAIN -> blocks deletion).
    """
    if account.is_sso is not True:
        return False
    if account.role == "superadmin":
        return False
    if not await tenant_repo.is_provider_account(session, account):
        return False
    if await member_repo.groups_containing_upn(session, account.username):
        return False
    if account.id is None:
        return False
    return not (
        await tenant_repo.list_grant_tenant_ids(session, account.id, "admin")
        or await tenant_repo.list_grant_tenant_ids(session, account.id, "auditor")
    )


async def sync_group(
    session: AsyncSession, settings: dict[str, Any], group_id: int
) -> dict[str, int]:
    """Synchronizes an `AssignmentGroup` (snapshot + grant materialization).

    Returns `{member_count, materialized, added, removed}`:
    - `member_count`: size of the new snapshot (members after the reconcile),
    - `materialized`: number of reconciled PROVIDER accounts (customer/NULL-home matches
      are a no-op via the gate and do NOT count),
    - `added`/`removed`: snapshot diff against the previous state.
    """
    group = await session.get(AssignmentGroup, group_id)
    if group is None:
        raise NotFoundError("Gruppe nicht gefunden.", code="group_not_found")

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )

    # NEVER pass Graph errors through as a 500 -- snapshot/grants remain untouched.
    try:
        members = await graph.get_group_members(group.entra_group_id)
    except GraphError as exc:
        raise GroupSyncError(
            f"Der Gruppen-Sync ist fehlgeschlagen: {exc.message}", code="sync_failed"
        ) from exc
    except Exception as exc:  # transport/unexpected -> also cleanly typed
        raise GroupSyncError(
            f"Der Gruppen-Sync ist fehlgeschlagen: {exc}", code="sync_failed"
        ) from exc

    # (1) Capture this group's OLD match set BEFORE the snapshot reconcile.
    old_upns = await member_repo.upns_for_group(session, group_id)

    # (2) Bring the snapshot up to the fetched set; set the group's sync timestamp.
    recon = await member_repo.reconcile_snapshot(session, group_id, members)
    group.last_synced_at = utcnow()

    # (3) NEW match set AFTER the reconcile; what gets reconciled is the UNION (OLD and
    # NEW), so a member removed in this run also loses its now-orphaned grant.
    new_upns = await member_repo.upns_for_group(session, group_id)

    materialized = 0
    for upn in old_upns | new_upns:
        account = await user_repo.get_by_username(session, upn)  # exact/case-sensitive
        if account is None:
            continue  # Unmatched -> snapshot only, no grant, no account creation.
        # Team set purely from local snapshots (post-reconcile), passed 1:1 to the vetted
        # reconcile.
        team = await member_repo.groups_containing_upn(session, upn)
        # The reconcile is now ROLE-AWARE: each team's role decides its customers' target
        # table (admin team -> admin_tenant, auditor team -> auditor_tenant, admin wins) --
        # the sync inherits this unchanged, no grant write path of its own.
        await assignment_group_repo.reconcile_group_grants(session, account, list(team))
        # `materialized` counts the effectively applied provider matches -- the gate
        # decision remains solely in `reconcile_group_grants`; this check is only for
        # counting.
        if await tenant_repo.is_provider_account(session, account):
            materialized += 1

    # (4) DEPROVISION CLEANUP -- SECURITY-CRITICAL, DELETES `app_user` rows.
    # Candidates are EXCLUSIVELY the ex-members of THIS run (`old_upns - new_upns`): a
    # member that left is by definition in OLD but not in NEW. The reconcile above has
    # already revoked their orphaned `source='group'` grant and updated the snapshot, so
    # the grant tables and `groups_containing_upn` reflect the final post-sync state -- the
    # gate therefore evaluates against the definitive truth.
    #
    # DELIBERATELY NO mass-/`removal_blocked` heuristic like in `oidc.sync_sso_users`: there,
    # a target state for an ENTIRE tenant is computed and safeguarded against a mass
    # deletion run. Here we delete at most the handful of accounts that left THIS ONE
    # group in THIS run -- each individually secured by the full fail-safe gate
    # (`_is_fully_deprovisioned`, default keep).
    for upn in old_upns - new_upns:
        account = await user_repo.get_by_username(session, upn)  # exact/case-sensitive
        if account is None:
            continue  # No local account for this UPN -> nothing to delete.
        if not await _is_fully_deprovisioned(session, account):
            continue  # One condition not met -> KEEP the account (fail-closed).
        assert account.id is not None
        # `user_repo.delete` first explicitly removes the account's `UserSession` rows (no
        # FK dangle). It no longer commits itself (M-03): the deletion and the following
        # `USER_DELETED` audit entry are only staged and land together in the caller's
        # commit (`admin_groups._auto_sync`/`sync_group_route`) -- this way a crash in
        # between never lets a deletion through without its audit entry (or vice versa).
        await user_repo.delete(session, account.id)
        await audit.record(
            session,
            action=audit.USER_DELETED,
            actor_type="system",
            target=upn,
            detail={"reason": "group_sync_deprovision", "group": group.name},
        )

    result = {
        "member_count": recon["total"],
        "materialized": materialized,
        "added": recon["added"],
        "removed": recon["removed"],
    }

    await audit.record(
        session,
        action=audit.GROUP_SYNCED,
        actor_type="system",
        target=group.name,
        detail={
            "member_count": result["member_count"],
            "materialized": result["materialized"],
            "added": result["added"],
            "removed": result["removed"],
        },
    )
    return result
