"""Synchronisiert Entra-Benutzer nach lokal und berechnet Ablaufdaten."""

from __future__ import annotations

import datetime as dt
from fnmatch import fnmatch
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from ...core.logging import get_logger
from ...repositories import entra_repo
from ..expiry import compute_expiry
from .client import GraphClient, GraphConfig

log = get_logger("graph.sync")


class SyncOutcome(TypedDict, total=False):
    checked: int
    skipped: str


def is_graph_configured(settings: dict[str, Any]) -> bool:
    """Graph gilt erst als konfiguriert, wenn Mandant, Client und Secret ALLE gesetzt sind.

    Fehlt eines, baut ``sync_users`` erst gar keinen ``GraphClient`` -- sonst entstünde
    eine MSAL-Authority ohne Mandanten-Segment (``.../login.microsoftonline.com/``) und
    ein roher, englischer MSAL-Fehler landete (zusätzlich noch doppelt) im Lauf-Protokoll.
    Spiegelt die Form von ``oidc.is_configured`` (``oidc.py``), aber bewusst OHNE die
    SSO-spezifischen Bedingungen (``oidc.enabled``/``oidc.admin_group_id``) -- die sind
    für den reinen Passwort-Sync irrelevant.
    """
    return bool(
        str(settings.get("graph.tenant_id") or "").strip()
        and str(settings.get("graph.client_id") or "").strip()
        and str(settings.get("graph.client_secret") or "").strip()
    )


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_shared_mailbox(
    raw: dict[str, Any],
    upn: str,
    mail: str | None,
    *,
    patterns: list[str],
    detect_unlicensed: bool,
) -> bool:
    """Erkennt Shared Mailboxes.

    Primär (empfohlen): Konto hat ein Postfach (``mail`` gesetzt), aber keine
    zugewiesene Lizenz -> Shared/Room/Equipment (normale User brauchen eine Lizenz
    fürs Postfach). Zusätzlich: optionale Glob-Muster als manueller Override.
    """
    if detect_unlicensed and mail and not (raw.get("assignedLicenses") or []):
        return True
    targets = [t.lower() for t in (upn, mail) if t]
    return any(fnmatch(t, p.lower()) for p in patterns for t in targets)


def resolve_validity(
    upn: str, override: int | None, default_validity: int | None, by_domain: dict[str, int]
) -> int | None:
    if override:
        return override
    suffix = upn.split("@")[-1].lower() if "@" in upn else ""
    if suffix in by_domain:
        return by_domain[suffix]
    return default_validity


async def sync_users(session: AsyncSession, settings: dict[str, Any]) -> SyncOutcome:
    if not is_graph_configured(settings):
        # Kein Token-Versuch ohne Mandant/Client/Secret -- sonst roher MSAL-Fehler
        # (siehe `is_graph_configured`-Docstring). `execute_run` verbucht das als
        # harmlosen `detail_log`-Eintrag, ohne `status="partial"`/`error`.
        log.info("graph_sync_skipped", reason="graph_not_configured")
        return {"checked": 0, "skipped": "graph_not_configured"}

    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )

    override = settings.get("policy.validity_days_override")
    auto = settings.get("policy.auto_detect", True)
    shared_patterns = settings.get("sync.shared_patterns") or []
    detect_unlicensed = bool(settings.get("sync.shared_detect_unlicensed", True))
    default_validity: int | None = None
    by_domain: dict[str, int] = {}
    if auto and not override:
        default_validity, by_domain = await graph.get_password_validity_map()

    # Sync-Umfang: konfigurierte Gruppe -> nur deren (transitive) Mitglieder, sonst alle.
    group_id = str(settings.get("sync.group_id") or "").strip()
    source = graph.iter_group_users(group_id) if group_id else graph.iter_users()

    now = dt.datetime.now(dt.UTC)
    checked = 0
    async for raw in source:
        upn = raw.get("userPrincipalName") or ""
        mail = raw.get("mail")
        last_change = _parse_dt(raw.get("lastPasswordChangeDateTime"))
        policies = raw.get("passwordPolicies")
        validity = resolve_validity(upn, override, default_validity, by_domain)
        result = compute_expiry(
            last_password_change=last_change,
            validity_days=validity,
            password_policies=policies,
            now=now,
        )
        await entra_repo.upsert(
            session,
            {
                "entra_id": raw["id"],
                "upn": upn,
                "display_name": raw.get("displayName") or "",
                "mail": mail,
                "is_shared": detect_shared_mailbox(
                    raw, upn, mail, patterns=shared_patterns, detect_unlicensed=detect_unlicensed
                ),
                "other_mails": raw.get("otherMails") or [],
                "account_enabled": bool(raw.get("accountEnabled", True)),
                "department": raw.get("department"),
                "job_title": raw.get("jobTitle"),
                "language": raw.get("preferredLanguage"),  # z. B. "de-CH", "en-US"
                "last_password_change": last_change,
                "password_policies": policies,
                "password_never_expires": result.never_expires,
                "expiry_date": result.expiry_date,
                "days_left": result.days_left,
                "raw": raw,
                "last_synced_at": now,
            },
        )
        checked += 1
        if checked % 200 == 0:
            await session.commit()
    await session.commit()
    log.info("graph_sync_done", checked=checked, default_validity=default_validity)
    return {"checked": checked}
