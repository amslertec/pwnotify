"""Audit-Protokoll: Aktionskennungen und das Schreiben von Einträgen.

Alle Aufrufer gehen über :func:`record`, damit Format und Redaction an einer Stelle
festgelegt sind. Der DB-Zugriff liegt in ``repositories/audit_repo.py``.

Das Protokollieren darf die auslösende Aktion nie scheitern lassen: Ein Fehler beim
Schreiben wird geloggt, aber nicht weitergereicht — ein kaputter Audit-Eintrag darf
keine Anmeldung verhindern.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..models.user import AppUser
from ..repositories import audit_repo

log = get_logger("audit")

# Stabile Kennungen. Sie sind Teil der API und werden im Frontend übersetzt — nicht
# umbenennen, ohne die Übersetzungen und bestehende Einträge mitzudenken.
LOGIN_SUCCESS = "auth.login_success"
LOGIN_FAILED = "auth.login_failed"
LOGIN_BLOCKED = "auth.login_blocked"  # gesperrtes Konto hat es erneut versucht
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
TENANT_ASSIGNED = "tenant.assigned"  # Zuweisung eines Kontos zu einem weiteren Mandanten
TENANT_UNASSIGNED = "tenant.unassigned"  # Zuweisung entzogen (Task 4)
SUPERADMIN_CREATED = "user.superadmin_created"
INSTANCE_MODE_CHANGED = "instance.mode_changed"  # Multi-Tenant-Mode umgeschaltet (Task 5)
GROUP_CREATED = "group.created"  # Assignment-Group angelegt (Console+Groups+Invite Task 3)
GROUP_UPDATED = "group.updated"  # Assignment-Group umbenannt
GROUP_DELETED = "group.deleted"
GROUP_TENANTS_SET = "group.tenants_set"  # Kunden-Mitgliedschaft einer Gruppe reconciled
GROUP_SYNCED = "group.synced"  # Entra-Gruppen-Sync: Snapshot + Grant-Materialisierung (Task 3)
USER_INVITED = "user.invited"  # Einladung verschickt (Console+Groups+Invite Task 5)
INVITATION_ACCEPTED = "user.invitation_accepted"  # öffentlicher Accept-Endpunkt
PASSWORD_RESET_SENT = "auth.password_reset_sent"  # Admin hat einen Reset-Link ausgelöst
PASSWORD_RESET_DONE = "auth.password_reset_done"  # öffentlicher Reset-Endpunkt

# Werte dieser Felder gehören nie ins Protokoll, auch nicht gekürzt: Es ist für Admins
# einsehbar und wird exportiert.
_NEVER_LOG = frozenset({"password", "secret", "client_secret", "smtp_password", "token", "code"})


def _clean(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    return {k: v for k, v in detail.items() if k.lower() not in _NEVER_LOG}


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
) -> None:
    """Einen Eintrag vormerken. Committet NICHT — das übernimmt der Aufrufer.

    ``actor_username`` erlaubt das Protokollieren fehlgeschlagener Anmeldungen, bei denen
    kein Benutzerobjekt existiert (etwa ein durchprobierter, unbekannter Name).
    """
    try:
        entry = audit_repo.build(
            actor_id=actor.id if actor else None,
            actor_username=(actor.username if actor else actor_username),
            actor_type=actor_type,
            action=action,
            target=target,
            outcome=outcome,
            ip_address=(request.client.host if request and request.client else None),
            user_agent=(request.headers.get("user-agent") if request else None),
            detail=_clean(detail),
        )
        session.add(entry)
    except Exception as exc:  # pragma: no cover — Protokoll darf nie die Aktion kippen
        log.error("audit_record_failed", action=action, error=str(exc))
