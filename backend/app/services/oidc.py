"""SSO/OIDC via Microsoft Entra (authorization code flow).

Uses the same app registration as Graph. Only members of the configured Entra
admin group may sign in -- the group check happens via the ``groups`` claim in
the ID token (app manifest: ``groupMembershipClaims``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import msal
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import decrypt, encrypt
from ..core.errors import AuthError, PwNotifyError
from ..core.logging import get_logger
from ..core.security import hash_password
from ..repositories import assignment_group_repo, tenant_repo, user_repo
from . import audit
from .graph import GraphClient, GraphConfig

log = get_logger("oidc")

_LOGIN = {
    "global": "https://login.microsoftonline.com",
    "usgov": "https://login.microsoftonline.us",
    "china": "https://login.chinacloudapi.cn",
}
_SCOPES = ["User.Read"]  # yields a valid token; groups claim comes from the manifest


@dataclass
class OidcResult:
    username: str
    display_name: str
    allowed: bool
    role: str = "admin"
    reason: str | None = None
    tid: str | None = None
    """Entra tenant ID (`tid` claim) of the ID token -- basis for the SSO tenant mapping
    (phase 4a task 4). ``None`` only if the token exceptionally does not include the claim."""
    groups: list[str] | None = None
    """Raw group claim (or Graph lookup result) of the token -- ``None`` only if no group
    information could be determined. Basis for the AUTHORITATIVE, per-customer role
    re-resolution in the callback (security fix, phase 4c task 4): `role`/`allowed` above
    are computed against the OWNER/instance settings (transitional, see below) and must
    NOT be adopted unchecked for the role in a customer found via `tid` once >=2 SSO
    customers exist -- `resolve_role(groups, tenant_settings)` must be called again for
    that."""


def is_configured(settings: dict[str, Any]) -> bool:
    return bool(
        settings.get("oidc.enabled")
        and settings.get("graph.tenant_id")
        and settings.get("graph.client_id")
        and settings.get("graph.client_secret")
        and settings.get("oidc.admin_group_id")
    )


def _authority(settings: dict[str, Any]) -> str:
    login = _LOGIN.get(settings.get("graph.cloud") or "global", _LOGIN["global"])
    return f"{login}/{settings.get('graph.tenant_id')}"


def _app(settings: dict[str, Any]) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        settings.get("graph.client_id"),
        authority=_authority(settings),
        client_credential=settings.get("graph.client_secret"),
    )


# -- Flow (MSAL native auth-code-flow: PKCE S256 + nonce + state, all built-in) ---------- #
def encode_flow_cookie(flow: dict[str, Any]) -> str:
    """Encrypt the MSAL auth-code flow dict for the short-lived, browser-bound state cookie."""
    return encrypt(json.dumps(flow))


def decode_flow_cookie(value: str) -> dict[str, Any]:
    """Decrypt+parse the flow cookie. Raises AuthError on any tampering/format error."""
    try:
        data: dict[str, Any] = json.loads(decrypt(value))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError(
            "Ungültiger oder abgelaufener SSO-State.", code="sso_state_invalid"
        ) from exc
    return data


def initiate_login(
    settings: dict[str, Any], redirect_uri: str, login_hint: str | None = None
) -> tuple[str, str]:
    """Start an OIDC auth-code flow with PKCE+nonce. Returns (auth_url, encrypted_flow_cookie)."""
    if not is_configured(settings):
        raise PwNotifyError("SSO ist nicht vollständig konfiguriert.", code="oidc_not_configured")
    # response_mode=form_post (RFC 9700 §4.3.1): Entra returns the authorization code in the
    # POST body of an auto-submitting form instead of appending it to the redirect URL. The code
    # then never lands in the URL bar, browser history, `Referer` headers, or reverse-proxy
    # access logs -- closing the code-leak surface of the default query response mode. The
    # matching consequence is that the callback becomes a cross-site POST; see the flow-cookie
    # docstring in `deps.set_oidc_flow_cookie` for the SameSite=None; Secure (HTTPS-only) fallout.
    flow = _app(settings).initiate_auth_code_flow(
        _SCOPES,
        redirect_uri=redirect_uri,
        prompt="select_account",
        login_hint=login_hint or None,
        response_mode="form_post",
    )
    return flow["auth_uri"], encode_flow_cookie(flow)


def resolve_role(
    groups: list[str] | None, settings: dict[str, Any]
) -> tuple[str, bool, str | None]:
    """Determines role + access of ONE per-customer settings dict against a group claim.

    Pure/stateless so it can be called twice: once in `exchange_and_verify` against the
    owner/instance settings (transitional path, the token exchange itself still runs via
    the instance-wide app registration), and a second time AUTHORITATIVELY in the callback
    against the settings OF the customer found via `tid` (security fix, phase 4c task 4) --
    only this second call decides `user.role`/admission. Admin group takes precedence over
    auditor group.

    Returns ``(role, allowed, reason)``; ``reason`` is ``None`` on success.
    """
    if groups is None:
        return (
            "admin",
            False,
            "Keine Gruppeninformationen im Token und Rückfrage bei Microsoft Graph "
            "nicht möglich. Bitte im App-Manifest 'groupMembershipClaims' auf "
            "'SecurityGroup' setzen.",
        )
    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    if admin_group and admin_group in groups:
        return "admin", True, None
    if auditor_group and auditor_group in groups:
        return "auditor", True, None
    return "admin", False, "Nicht Mitglied einer berechtigten Gruppe."


async def resolve_group_role(session: AsyncSession, groups: list[str] | None) -> tuple[str, bool]:
    """Role + access for PROVIDER staff in multi-tenant mode, derived from TEAM membership.

    WHY it exists: SSO is a PROVIDER feature in multi-tenant mode. When an instance runs
    in multi-tenant mode, it is no longer the per-customer settings role group
    (`resolve_role`) that decides admission and role, but membership in a team
    (`AssignmentGroup`) -- all SSO staff is managed via teams, not via the
    `oidc.admin_group_id`/`oidc.auditor_group_id` settings. This function is reachable
    behind the callback gate for EVERY multi-tenant SSO login (`if multi_tenant`,
    regardless of the matched `tid`); the account always homes on the default tenant in
    that case. Only in SINGLE-tenant mode does the login stay with `resolve_role`.

    Fail-closed WITHOUT a settings fallback: no token `groups` claim, or NO claim pointing
    at ANY team, means NOT authorized (`("admin", False)`). Otherwise admin wins -- if the
    claim matches an admin team, the role becomes `"admin"`, otherwise (only auditor
    teams) `"auditor"`.

    Returns `(role, allowed)` -- NO `reason` (the callback supplies the fixed reason string
    itself on denial)."""
    if not groups:
        return "admin", False
    roles = await assignment_group_repo.group_roles_for_entra_groups(session, set(groups))
    if not roles:
        return "admin", False
    if "admin" in roles:
        return "admin", True
    return "auditor", True


async def exchange_and_verify(
    settings: dict[str, Any], flow: dict[str, Any], auth_response: dict[str, Any]
) -> OidcResult:
    """Complete the MSAL auth-code flow started by :func:`initiate_login`.

    Delegates state match (CSRF), nonce match against the id_token (replay), and the PKCE
    ``code_verifier`` exchange entirely to MSAL in one call -- a ``state`` mismatch raises
    ``ValueError`` before any network call (verified against msal 1.37.0).
    """
    app = _app(settings)
    try:
        result = await asyncio.to_thread(app.acquire_token_by_auth_code_flow, flow, auth_response)
    except ValueError as exc:  # MSAL raises on state mismatch before any network call
        raise AuthError("SSO-State stimmt nicht überein.", code="sso_state_mismatch") from exc
    if "access_token" not in result:
        desc = result.get("error_description", result.get("error", "unbekannt"))
        raise AuthError(desc, code="sso_token_exchange_failed")

    claims: dict[str, Any] = result.get("id_token_claims", {})
    username = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""
    display_name = claims.get("name") or username
    tid = claims.get("tid")

    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    groups = claims.get("groups")
    if not isinstance(groups, list):
        # Two causes: 'groupMembershipClaims' is not set in the app manifest -- or the
        # user is a member of more than 200 groups, in which case Entra returns only a
        # reference ("overage") instead of the list. The latter happens to affect accounts
        # with many memberships, i.e. typically administrators. This is why we specifically
        # query here instead of rejecting the sign-in outright.
        groups = await _groups_via_graph(
            settings, claims.get("oid") or username, [admin_group, auditor_group]
        )
    role, allowed, reason = resolve_role(groups, settings)
    return OidcResult(
        username=username,
        display_name=display_name,
        allowed=allowed,
        role=role,
        reason=reason,
        tid=tid,
        groups=groups,
    )


async def _groups_via_graph(
    settings: dict[str, Any], user_id: str, group_ids: list[str]
) -> list[str] | None:
    """Query membership directly from Graph when the token does not supply groups.

    Returns the subset of the checked groups, ``None`` if the query was not possible (no
    secret, no groups configured, Graph error) -- in that case the previous behavior
    applies: reject sign-in instead of granting access when in doubt.
    """
    gesucht = [g for g in group_ids if g]
    if not (user_id and gesucht and settings.get("graph.client_secret")):
        return None
    graph = GraphClient(
        GraphConfig(
            tenant_id=str(settings.get("graph.tenant_id") or ""),
            client_id=str(settings.get("graph.client_id") or ""),
            client_secret=str(settings.get("graph.client_secret") or ""),
            cloud=str(settings.get("graph.cloud") or "global"),
        )
    )
    try:
        treffer = await graph.check_member_groups(user_id, gesucht)
        log.info("oidc_groups_via_graph", user=user_id, matched=len(treffer))
        return list(treffer)
    except Exception as exc:
        log.warning("oidc_group_lookup_failed", user=user_id, error=str(exc))
        return None
    finally:
        await graph.aclose()


_MAX_REMOVAL_RATIO = 0.5
"""Threshold ratio above which a sync is treated as a misconfiguration rather than a departure."""


def removal_blocked_reason(
    *, desired_count: int, existing_count: int, removal_count: int
) -> str | None:
    """Checks whether a planned deletion is plausible. Returns the reason if not.

    The sync removes SSO users who are no longer in any authorized group. Two cases are
    almost always a configuration error rather than a genuine departure -- and both end
    with the operator locking themselves out of their own application:

    * The desired set is empty (emptied group, wrong group ID): nobody intentionally
      removes all admins at once.
    * The sync would remove the majority of all SSO users.

    When in doubt, nothing is deleted: keeping one user too many is fixable, locking out
    all administrators is not.
    """
    if removal_count == 0:
        return None
    if existing_count == 0:
        return None
    if desired_count == 0:
        return (
            "Die Admin-/Auditor-Gruppe ist leer — kein Abgleich möglich. "
            "Es wurde nichts entfernt (Schutz vor Aussperrung). "
            "Gruppen-ID und Mitgliedschaften in den Einstellungen prüfen."
        )
    if removal_count > existing_count * _MAX_REMOVAL_RATIO:
        return (
            f"Der Abgleich würde {removal_count} von {existing_count} SSO-Benutzern "
            "entfernen — das sieht nach einer Fehlkonfiguration aus. Es wurde nichts "
            "entfernt (Schutz vor Aussperrung)."
        )
    return None


async def sync_sso_users(
    session: AsyncSession, settings: dict[str, Any], *, tenant_id: int
) -> dict[str, int]:
    """Reconciles the SSO users of ONE tenant against its Entra admin and auditor group.

    Members of the admin group receive the role ``admin``, members of the auditor group
    ``auditor`` (admin takes precedence). Former SSO users who are no longer in either
    group are removed -- safeguarded by :func:`removal_blocked_reason`.

    Security fix: both account creation (``tenant_id`` on the new account) and the removal
    set (``list_sso_for_tenant`` instead of the instance-wide ``list_sso``) are strictly
    scoped to ``tenant_id``. Previously, a sync for customer A would have seen the SSO
    accounts of EVERY other customer as "no longer in any group" (their UPNs are of course
    not in A's ``desired``) and deleted them -- including their administrators.
    `tenant_id` is therefore a required parameter, not a default: a call without an active
    tenant must fail/be skipped by the caller, not silently run instance-wide here.
    """
    admin_group = str(settings.get("oidc.admin_group_id") or "")
    auditor_group = str(settings.get("oidc.auditor_group_id") or "")
    if not (settings.get("oidc.enabled") and admin_group and settings.get("graph.client_secret")):
        return {"synced": 0, "removed": 0}

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )

    # upn_lower -> (original UPN, role, display name); admin group overrides auditor.
    desired: dict[str, tuple[str, str, str]] = {}
    if auditor_group:
        for m in await graph.get_group_members(auditor_group):
            upn = m.get("userPrincipalName")
            if upn:
                desired[upn.lower()] = (upn, "auditor", m.get("displayName") or upn)
    for m in await graph.get_group_members(admin_group):
        upn = m.get("userPrincipalName")
        if upn:
            desired[upn.lower()] = (upn, "admin", m.get("displayName") or upn)

    synced = 0
    for upn, role, name in desired.values():
        user = await user_repo.get_by_username(session, upn)
        if user is None:
            await user_repo.create(
                session,
                username=upn,
                password_hash=hash_password(uuid.uuid4().hex),
                display_name=name,
                role=role,
                is_sso=True,
                tenant_id=tenant_id,
            )
        elif not user.is_sso:
            # A local (non-SSO) account owns this UPN. Never adopt it into SSO (would silently
            # let group membership take over a local admin/superadmin). Skip it entirely.
            log.warning("sso_sync_local_account_conflict", username=upn, role=user.role)
            continue
        elif user.tenant_id != tenant_id:
            # M8: an SSO account homed in a DIFFERENT tenant happens to share this UPN. A
            # customer admin controls their own tenant's group membership, hence this `desired`
            # set -- letting it overwrite another tenant's account (role, activation) would be a
            # cross-tenant takeover. Skip entirely; the account stays owned by its home tenant.
            log.warning(
                "sso_sync_foreign_tenant_conflict",
                username=upn,
                home_tenant=user.tenant_id,
                sync_tenant=tenant_id,
            )
            continue
        else:
            user.is_sso = True
            user.display_name = name
            user.role = role
            # L8: do NOT force is_active=True on every sync. Presence is governed by the removal
            # pass below (absent from the desired group -> deleted). Blindly reactivating would
            # silently revive a deliberately deactivated account once a deactivation path exists.
        synced += 1

    # Remove SSO users who are no longer in any authorized group -- ONLY those of THIS
    # tenant (see security-fix note in the docstring above). We keep the account objects
    # (not just the ids): we need the UPN for the audit entry (M-02) and role/tenant
    # membership for the last-admin backstop (L-03).
    existing_sso = await user_repo.list_sso_for_tenant(session, tenant_id)
    removable = [u for u in existing_sso if u.username.lower() not in desired]

    blocked = removal_blocked_reason(
        desired_count=len(desired),
        existing_count=len(existing_sso),
        removal_count=len(removable),
    )
    if blocked:
        await session.commit()  # keep the role/name reconcile above, just don't delete
        log.error(
            "sso_removal_blocked",
            reason=blocked,
            desired=len(desired),
            existing=len(existing_sso),
            would_remove=len(removable),
        )
        return {"synced": synced, "removed": 0, "removal_blocked": 1}

    removed = 0
    admin_protected = 0
    for u in removable:
        if u.id is None:
            continue
        # L-03: last-admin backstop -- NEVER deprovision the sole admin of a tenant, even
        # if the removal ratio would allow it. Consistent with A4 (set_role/delete_user): a
        # last admin lost via SSO group departure would otherwise lock the tenant out.
        protected = False
        for tid in await tenant_repo.admin_tenants(session, u):
            if await user_repo.count_tenant_admins(session, tid) <= 1:
                protected = True
                break
        if protected:
            admin_protected += 1
            log.warning("sso_removal_admin_protected", username=u.username, sync_tenant=tenant_id)
            continue
        upn = u.username
        await user_repo.delete(session, u.id)
        # M-02: a deletion is never silent. Atomic with the deletion, since
        # `user_repo.delete` no longer commits itself (M-03) and this sync commits once
        # below.
        await audit.record(
            session,
            action=audit.USER_DELETED,
            actor_type="system",
            target=upn,
            tenant_id=tenant_id,
            detail={"reason": "sso_sync_deprovision"},
        )
        removed += 1

    # M-02: an aggregate SSO_SYNCED so a SCHEDULED sync also leaves a trace (previously
    # only the manual route wrote that). Attributed to the synced tenant. Also written on
    # admin_protected, so a run that ONLY protected an admin remains visible.
    if synced or removed or admin_protected:
        await audit.record(
            session,
            action=audit.SSO_SYNCED,
            actor_type="system",
            tenant_id=tenant_id,
            detail={"synced": synced, "removed": removed, "admin_protected": admin_protected},
        )

    await session.commit()
    log.info("sso_users_synced", synced=synced, removed=removed, admin_protected=admin_protected)
    return {"synced": synced, "removed": removed, "admin_protected": admin_protected}
