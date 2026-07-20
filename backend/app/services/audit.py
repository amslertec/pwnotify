"""Audit log: action identifiers and writing entries.

All callers go through :func:`record`, so format and redaction are pinned down in one
place. DB access lives in ``repositories/audit_repo.py``.

Logging must never fail the triggering action: an error while writing is logged but not
re-raised — a broken audit entry must not block a login.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.http import client_ip, client_user_agent
from ..core.logging import get_logger
from ..core.redaction import is_secret_key
from ..models.user import AppUser
from ..repositories import audit_repo

log = get_logger("audit")

# Stable identifiers. They are part of the API and get translated in the frontend — do not
# rename without considering the translations and existing entries.
LOGIN_SUCCESS = "auth.login_success"
LOGIN_FAILED = "auth.login_failed"
LOGIN_BLOCKED = "auth.login_blocked"  # locked account tried again
LOGOUT = "auth.logout"
ACCOUNT_LOCKED = "auth.account_locked"
PASSWORD_CHANGED = "auth.password_changed"
TWOFA_ENABLED = "auth.2fa_enabled"
TWOFA_DISABLED = "auth.2fa_disabled"
SESSIONS_REVOKED = "auth.sessions_revoked"
TENANT_SWITCHED = "auth.tenant_switched"
USER_CREATED = "user.created"
USER_DELETED = "user.deleted"
USER_ROLE_CHANGED = "user.role_changed"
SETTINGS_CHANGED = "settings.changed"
SECRET_CHANGED = "settings.secret_changed"
TENANT_CREATED = "tenant.created"
TENANT_UPDATED = "tenant.updated"
TENANT_DELETED = "tenant.deleted"
TENANT_ASSIGNED = "tenant.assigned"  # account assigned to an additional tenant
TENANT_UNASSIGNED = "tenant.unassigned"  # assignment revoked (Task 4)
SUPERADMIN_CREATED = "user.superadmin_created"
INSTANCE_MODE_CHANGED = "instance.mode_changed"  # multi-tenant mode toggled (Task 5)
GROUP_CREATED = "group.created"  # assignment group created (Console+Groups+Invite Task 3)
GROUP_UPDATED = "group.updated"  # assignment group renamed
GROUP_DELETED = "group.deleted"
GROUP_TENANTS_SET = "group.tenants_set"  # customer membership of a group reconciled
GROUP_SYNCED = "group.synced"  # Entra group sync: snapshot + grant materialization (Task 3)
USER_INVITED = "user.invited"  # invitation sent (Console+Groups+Invite Task 5)
INVITATION_ACCEPTED = "user.invitation_accepted"  # public accept endpoint
PASSWORD_RESET_SENT = "auth.password_reset_sent"  # admin triggered a reset link
PASSWORD_RESET_DONE = "auth.password_reset_done"  # public reset endpoint

# Security Phase 5, Task 8 (M10): coverage for the remaining security-relevant routes that
# previously wrote no audit entry at all.
USER_EXCLUDED = "entra_user.exclusion_changed"  # exclude/include -- single, bulk, or via settings
NOTIFICATION_SENT_MANUAL = "notification.manual_send"  # single or bulk manual reminder
NOTIFICATION_RETRIED = "notification.retried"
RUN_TRIGGERED = "run.triggered"
BRANDING_CHANGED = "branding.changed"  # logo/favicon upload or delete
SSO_SYNCED = "user.sso_synced"
TWOFA_SETUP_STARTED = "auth.2fa_setup_started"  # secret/QR issued, not yet confirmed

# M3: a retention purge that actually removed audit rows -- deleting audit history must never
# be silent. Written by the runner with the deleted count in `detail`.
AUDIT_PURGED = "audit.purged"

# M7: a test mail dispatched via `/settings/mail/test` over the customer's own mail identity
# to an arbitrary recipient -- previously left no trace at all. Recipient goes into `detail`.
MAIL_TEST_SENT = "settings.mail_test_sent"

# Finding L3: coverage for routes that previously wrote no audit entry at all. `USERS_EXPORTED`
# is the important one -- a full-tenant PII export (`GET /users/export`) that left no trace.
# The others are low-signal self-service changes the auditor still lists.
USERS_EXPORTED = "entra_user.exported"  # mass-PII export (CSV/XLSX) -- count/format only
TEMPLATE_RESET = "settings.template_reset"  # notification templates reset to defaults
PROFILE_UPDATED = "auth.profile_updated"  # self-service display name/email change
LANGUAGE_CHANGED = "auth.language_changed"  # self-service UI language change
AVATAR_CHANGED = "auth.avatar_changed"  # self-service avatar upload/delete


def _clean_value(value: Any) -> Any:
    """Drop secret keys recursively (shared predicate `is_secret_key`, finding I1) so a
    nested secret key inside a ``detail`` sub-dict is dropped too, not only at the top level.
    The audit log is admin-readable and exportable -- a secret value must never reach it."""
    if isinstance(value, dict):
        return {k: _clean_value(v) for k, v in value.items() if not is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return type(value)(_clean_value(v) for v in value)
    return value


def _clean(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    return {k: _clean_value(v) for k, v in detail.items() if not is_secret_key(k)}


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor: AppUser | None = None,
    actor_username: str | None = None,
    actor_type: str = "user",
    target: str | None = None,
    outcome: str = "success",
    request: Request | None = None,
    detail: dict[str, Any] | None = None,
    tenant_id: int | None = None,
) -> None:
    """Stage an entry. Does NOT commit — that is the caller's responsibility.

    ``actor_username`` allows logging failed logins where no user object exists (e.g. an
    attempted, unknown username).

    ``tenant_id`` (Security Phase 5, Task 7/M11): explicit tenant attribution for
    OWNER-SESSION callers, where `AuditLog.tenant_id`'s `default_factory` (the active
    tenant `ContextVar`) has nothing to stamp. Pass it only when the action is clearly
    attributable to ONE customer -- e.g. the admin-user-management routes attribute to the
    target account's home tenant, and `LOGIN_SUCCESS` attributes to the tenant the session
    actually logged into. Deliberately NOT set for: provider-level actions (superadmin
    management, tenant/instance CRUD -- no single customer), login FAILURES (the account,
    and therefore its tenant, may not be reliably known), and self-service auth actions
    (`password_changed`, `2fa_*`, `logout`) -- left `None` (NULL) unless a future task
    decides otherwise. On a tenant-scoped session this is normally left unset -- the
    `default_factory` already stamps correctly there.

    Note on the `None` case: the `tenant_id` override is intended for owner-session routes
    that need to attribute an otherwise NULL-tenant event to a customer. It cannot force a
    NULL stamp on a tenant-scoped session: `tenant_id=None` falls back to the ContextVar
    default (the active tenant). Pass an explicit int to attribute; omit it to use the
    ambient tenant.
    """
    try:
        entry = audit_repo.build(
            actor_id=actor.id if actor else None,
            actor_username=(actor.username if actor else actor_username),
            actor_type=actor_type,
            action=action,
            target=target,
            outcome=outcome,
            ip_address=client_ip(request),
            user_agent=client_user_agent(request),
            detail=_clean(detail),
            tenant_id=tenant_id,
            stamp_tenant=tenant_id is not None,
        )
        session.add(entry)
    except Exception as exc:  # pragma: no cover — logging must never tip over the action
        log.error("audit_record_failed", action=action, error=str(exc))
