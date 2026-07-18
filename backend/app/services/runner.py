"""Orchestriert einen kompletten Lauf: Graph-Sync -> Benachrichtigungen -> Run-Protokoll.

Fehler einzelner User oder eines Teilschritts dürfen den Lauf nie abbrechen.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.logging import get_logger
from ..db.tenant_context import current_tenant_or_none, use_owner_context
from ..models.run import Run
from ..repositories import (
    audit_repo,
    entra_repo,
    exclusion_repo,
    notification_repo,
    run_repo,
)
from . import alerts, retention
from .expiry import due_reminder_stage
from .graph import GraphClient, GraphConfig
from .graph.sync import sync_users
from .mail import build_sender
from .notifier import notify_user
from .settings_service import SettingsService, effective_base_url

log = get_logger("runner")

# Unterhalb dieser Menge wird nie blockiert: bei wenigen Konten ist ein hoher Anteil
# Fälliger normal (3 von 5 = 60 %) und harmlos.
_MASS_SEND_MIN_COUNT = 20


def mass_send_blocked_reason(*, due: int, checked: int, max_ratio: float) -> str | None:
    """Prüft, ob ein Lauf verdächtig viele Benachrichtigungen verschicken würde.

    Ein einzelner Konfigurationsfehler — etwa eine falsche Gültigkeitsdauer — lässt
    schlagartig alle Konten als fällig erscheinen. Ohne Bremse gingen dann tausende
    Mails an echte Empfänger; das ist nicht rückholbar und beim Kunden ein Vertrauens-
    schaden. Gibt den Grund zurück, wenn abgebrochen werden soll, sonst ``None``.
    """
    if max_ratio <= 0 or due == 0 or checked == 0:
        return None
    if due < _MASS_SEND_MIN_COUNT:
        return None
    if due > checked * max_ratio:
        return (
            f"Der Lauf würde {due} von {checked} Benutzern benachrichtigen "
            f"({due / checked:.0%}, erlaubt sind {max_ratio:.0%}). Das deutet auf eine "
            "Fehlkonfiguration hin (z. B. Gültigkeitsdauer oder Sync-Gruppe). Es wurde "
            "nichts versendet. Einstellungen prüfen oder einen Testlauf (Dry-Run) starten."
        )
    return None


async def _apply_privacy_retention(
    session: Any, settings: dict[str, Any], *, sync_ok: bool
) -> list[dict[str, Any]]:
    """Fristen für personenbezogene Daten anwenden. Gibt Protokollschritte zurück.

    ``sync_ok`` ist die entscheidende Sicherung: Nur wenn der Graph-Sync in diesem Lauf
    sauber durchlief, ist ``last_synced_at`` aussagekräftig. Nach einem gescheiterten Sync
    wirken alle Konten veraltet — Löschen wäre dann eine Katastrophe mit Ansage.
    """
    schritte: list[dict[str, Any]] = []

    user_tage = int(settings.get("privacy.user_retention_days") or 0)
    if user_tage > 0 and sync_ok:
        faellig = await entra_repo.count_stale(session, days=user_tage)
        gesamt = await entra_repo.count_all(session)
        grund = retention.purge_blocked_reason(to_delete=faellig, total=gesamt)
        if grund:
            log.error("user_retention_blocked", to_delete=faellig, total=gesamt)
            schritte.append({"step": "user_retention", "blocked": True, "reason": grund})
        elif faellig:
            entfernt = await entra_repo.delete_stale(session, days=user_tage)
            log.info("user_retention_applied", removed=entfernt, days=user_tage)
            schritte.append({"step": "user_retention", "removed": entfernt})
    elif user_tage > 0 and not sync_ok:
        log.warning("user_retention_skipped", reason="sync_failed")
        schritte.append({"step": "user_retention", "skipped": "sync_failed"})

    log_tage = int(settings.get("privacy.log_retention_days") or 0)
    if log_tage > 0:
        # Historie: unabhängig vom Sync, das Alter der Einträge stimmt immer.
        entfernt = await notification_repo.delete_older_than(session, days=log_tage)
        if entfernt:
            log.info("log_retention_applied", removed=entfernt, days=log_tage)
            schritte.append({"step": "log_retention", "removed": entfernt})
    return schritte


async def _resolve_excluded_ids(session: Any, settings: dict[str, Any]) -> set[str]:
    """Ausschluss-IDs aus den Regeln (User-Werte + transitive Gruppenmitglieder)."""
    excluded: set[str] = set(await exclusion_repo.user_values(session))
    group_ids = await exclusion_repo.group_ids(session)
    if group_ids:
        graph = GraphClient(
            GraphConfig(
                tenant_id=settings.get("graph.tenant_id") or "",
                client_id=settings.get("graph.client_id") or "",
                client_secret=settings.get("graph.client_secret") or "",
                cloud=settings.get("graph.cloud") or "global",
            )
        )
        for gid in group_ids:
            try:
                excluded |= await graph.get_group_member_ids(gid)
            except Exception as exc:
                log.warning("group_exclusion_failed", group=gid, error=str(exc))
    return excluded


async def execute_run(
    session_factory: async_sessionmaker[Any],
    *,
    trigger: str = "schedule",
    dry_run_override: bool | None = None,
    base_url: str = "http://localhost:8080",
) -> Run:
    async with session_factory() as session:
        svc = SettingsService(session)
        settings = await svc.get_all()
        base_url = effective_base_url(settings)
        dry_run = (
            dry_run_override
            if dry_run_override is not None
            else bool(settings.get("schedule.dry_run"))
        )
        reminder_days = settings.get("schedule.reminder_days") or [14, 7, 3, 1, 0]

        run = await run_repo.create(session, trigger=trigger, dry_run=dry_run)
        detail: list[dict[str, Any]] = []
        sent = failed = skipped = checked = 0
        status = "success"
        error: str | None = None
        started = dt.datetime.now(dt.UTC)
        # Vorbelegt, damit das finally auch greift, wenn der Lauf vorher scheitert.
        sender: Any = None

        try:
            # 1) Graph-Sync
            try:
                stats = await sync_users(session, settings)
                if stats.get("skipped") == "graph_not_configured":
                    # Kein MSAL-Token-Versuch ohne Graph-Konfiguration -- kein Fehler,
                    # der Lauf bleibt "success". Sichtbar wird das ausschliesslich über
                    # diesen `detail_log`-Eintrag, NICHT zusätzlich als `run.error`
                    # (sonst erschiene die Meldung doppelt).
                    detail.append({"step": "sync", "skipped": "graph_not_configured"})
                    log.info("run_sync_skipped", reason="graph_not_configured")
                else:
                    detail.append({"step": "sync", "checked": stats["checked"]})
            except Exception as exc:
                status = "partial"
                error = f"Sync-Fehler: {exc}"
                detail.append({"step": "sync", "error": str(exc)})
                log.error("run_sync_failed", error=str(exc))

            # 1b) SSO-Benutzer mit der Admin-Gruppe abgleichen (best effort).
            # WICHTIG: `app_user` ist instanzweit (kein tenant_id-RLS) -- läuft daher
            # bewusst auf einer eigenen Owner-Session, NICHT auf der tenant-gescopten
            # `session` dieses Laufs (die Einstellungen bleiben trotzdem die des
            # aktiven Tenants, aus `settings` oben gelesen).
            # Sicherheitsfix: `sync_sso_users` braucht den aktiven Tenant, um Anlegen UND
            # Entfernen strikt auf diesen Kunden zu scopen (sonst sähe es instanzweit auch
            # SSO-Konten anderer Kunden als "nicht mehr in der Gruppe" an und löschte sie).
            # Der Tenant MUSS hier, VOR `use_owner_context()`, gelesen werden -- der Wechsel
            # in den Owner-Kontext setzt das ContextVar für die Dauer des Blocks auf `None`.
            tenant_id_for_sso = current_tenant_or_none()
            if tenant_id_for_sso is None:
                # Kein aktiver Tenant (z. B. Owner-/Single-Tenant-Bootpfad) -- ohne Tenant
                # ist der Sync nicht sicher scopebar, also überspringen statt zu raten.
                log.warning("sso_sync_skipped", reason="no_active_tenant")
            else:
                try:
                    from . import oidc

                    with use_owner_context():
                        async with session_factory() as owner_session:
                            sso_stats = await oidc.sync_sso_users(
                                owner_session, settings, tenant_id=tenant_id_for_sso
                            )
                    if sso_stats.get("removal_blocked"):
                        # Sichtbar machen: ein blockierter Abgleich heisst, dass die
                        # Gruppenkonfiguration nicht stimmt. Der Lauf darf dann nicht
                        # "success" melden, sonst bleibt die Fehlkonfiguration unbemerkt —
                        # "partial" löst zusätzlich den Admin-Alert aus.
                        status = "partial"
                        detail.append({"step": "sso_sync", **sso_stats})
                    elif sso_stats["synced"] or sso_stats["removed"]:
                        detail.append({"step": "sso_sync", **sso_stats})
                except Exception as exc:
                    detail.append({"step": "sso_sync", "error": str(exc)})
                    log.warning("sso_sync_failed", error=str(exc))

            # 1c) Aufbewahrungsfristen anwenden (alle standardmässig aus).
            try:
                geloescht = await audit_repo.purge_older_than(
                    session, days=int(settings.get("audit.retention_days") or 0)
                )
                if geloescht:
                    detail.append({"step": "audit_purge", "removed": geloescht})
            except Exception as exc:
                log.warning("audit_purge_failed", error=str(exc))

            # Ausgeschiedene Konten nur entfernen, wenn der Sync ODER die Frist es hergibt.
            # Wichtig: Bei einem gescheiterten Sync altern alle Einträge gleichzeitig —
            # dann darf hier nichts gelöscht werden, sonst räumt eine Störung den Bestand.
            try:
                for schritt in await _apply_privacy_retention(
                    session, settings, sync_ok=status == "success"
                ):
                    detail.append(schritt)
            except Exception as exc:
                log.warning("privacy_retention_failed", error=str(exc))

            # 2) Benachrichtigungen
            excluded_ids = await _resolve_excluded_ids(session, settings)
            sender = build_sender(settings)
            users = await entra_repo.iter_active_for_notification(session)
            checked = len(users)

            # Vor dem ersten Versand abschätzen, wie viele Mails anstünden. Bewusst ohne
            # die Dedup-Abfrage (eine DB-Abfrage je Benutzer) — als Obergrenze reicht das,
            # und genau der Fehlerfall "plötzlich sind alle fällig" wird so erkannt,
            # bevor die erste Mail rausgeht.
            due_estimate = sum(
                1
                for u in users
                if u.days_left is not None
                and due_reminder_stage(
                    days_left=u.days_left, reminder_days=reminder_days, already_sent=set()
                )
                is not None
            )
            mass_block = mass_send_blocked_reason(
                due=due_estimate,
                checked=checked,
                max_ratio=float(settings.get("schedule.max_notify_ratio") or 0),
            )
            if mass_block and not dry_run:
                status = "partial"
                detail.append({"step": "mass_send_guard", "blocked": True, "reason": mass_block})
                log.error("mass_send_blocked", due=due_estimate, checked=checked)
                users = []

            for user in users:
                try:
                    outcome = await notify_user(
                        session,
                        user,
                        settings=settings,
                        sender=sender,
                        base_url=base_url,
                        reminder_days=reminder_days,
                        excluded_ids=excluded_ids,
                        dry_run=dry_run,
                        run_id=run.id,
                    )
                except Exception as exc:
                    failed += 1
                    detail.append({"upn": user.upn, "action": "error", "error": str(exc)})
                    log.error("notify_user_crashed", upn=user.upn, error=str(exc))
                    continue

                if outcome.action in ("sent", "dry_run"):
                    sent += 1
                    detail.append(
                        {
                            "upn": user.upn,
                            "action": outcome.action,
                            "stage": outcome.stage,
                            "recipient": outcome.recipient,
                            "channel": outcome.channel,
                        }
                    )
                elif outcome.action == "failed":
                    failed += 1
                    detail.append(
                        {
                            "upn": user.upn,
                            "action": "failed",
                            "stage": outcome.stage,
                            "error": outcome.error,
                        }
                    )
                else:
                    skipped += 1
        except Exception as exc:
            status = "error"
            error = str(exc)
            log.error("run_failed", error=str(exc))
        finally:
            # Gepoolte Verbindungen des Mail-Versands freigeben — sonst bleiben sie über
            # den Lauf hinaus offen. Auch im Fehlerfall.
            close = getattr(getattr(sender, "client", None), "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

        if failed and status == "success":
            status = "partial"

        finished = dt.datetime.now(dt.UTC)
        run.finished_at = finished
        run.duration_ms = int((finished - started).total_seconds() * 1000)
        run.checked_users = checked
        run.sent = sent
        run.failed = failed
        run.skipped = skipped
        run.status = status
        run.error = error
        run.detail_log = detail
        await session.commit()
        await session.refresh(run)
        log.info(
            "run_done",
            run_id=run.id,
            status=status,
            checked=checked,
            sent=sent,
            failed=failed,
            skipped=skipped,
            dry_run=dry_run,
        )
        # Admin-Digest / Fehler-Alert (best effort — beeinflusst den Lauf nie).
        try:
            await alerts.maybe_send_run_alert(session, settings, run, base_url)
        except Exception as exc:
            log.warning("run_alert_failed", error=str(exc))
        return run
