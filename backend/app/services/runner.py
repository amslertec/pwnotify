"""Orchestrates a complete run: Graph sync -> notifications -> run log.

Errors from a single user or a sub-step must never abort the run.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
from . import alerts, audit, retention
from .expiry import due_reminder_stage
from .graph import GraphClient, GraphConfig
from .graph.sync import is_graph_configured, sync_users
from .mail import build_sender
from .notifier import notify_user
from .settings_service import SettingsService, effective_base_url

log = get_logger("runner")

# Below this count, blocking never happens: with few accounts, a high ratio of due
# accounts is normal (3 of 5 = 60%) and harmless.
_MASS_SEND_MIN_COUNT = 20


def mass_send_blocked_reason(
    *, due: int, checked: int, max_ratio: float, max_count: int | None = None
) -> str | None:
    """Checks whether a run would send a suspiciously large number of notifications.

    A single configuration error -- e.g. a wrong validity period -- can suddenly make all
    accounts appear due. Without a brake, that would send thousands of emails to real
    recipients; that is not undoable and a trust-damaging incident with the customer.
    Returns the reason if the run should be aborted, otherwise ``None``.

    Two brakes: the absolute cap (``max_count``) canNOT be switched off via the ratio and
    also applies to very large datasets; the ratio brake (``max_ratio``) catches the
    "suddenly everything is due" case for smaller datasets.
    """
    if due == 0 or checked == 0:
        return None
    # Absolute ceiling — a second brake that cannot be switched off via the ratio.
    if max_count is not None and max_count > 0 and due > max_count:
        return (
            f"Der Lauf würde {due} Benutzer benachrichtigen — mehr als das absolute Limit "
            f"von {max_count}. Das deutet auf eine Fehlkonfiguration hin (z. B. Gültigkeits-"
            "dauer oder Sync-Gruppe). Es wurde nichts versendet. Einstellungen prüfen oder "
            "einen Testlauf (Dry-Run) starten."
        )
    # Ratio brake (small datasets below the floor are never blocked).
    if max_ratio > 0 and due >= _MASS_SEND_MIN_COUNT and due > checked * max_ratio:
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
    """Apply retention periods for personal data. Returns log steps.

    ``sync_ok`` is the decisive safeguard: ``last_synced_at`` is only meaningful if the
    Graph sync completed cleanly in this run. After a failed sync, all accounts look
    stale -- deleting them then would be a disaster waiting to happen.
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
        # History: independent of the sync, the age of the entries is always accurate.
        entfernt = await notification_repo.delete_older_than(session, days=log_tage)
        if entfernt:
            log.info("log_retention_applied", removed=entfernt, days=log_tage)
            schritte.append({"step": "log_retention", "removed": entfernt})
    return schritte


async def _apply_audit_retention(session: Any, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Purge old audit-log entries per ``audit.retention_days`` and audit that purge itself.

    Deleting audit history must never be silent (M3), so a real purge writes an
    ``AUDIT_PURGED`` entry with the deleted count. Tenant attribution: written on the run's
    session with no explicit ``tenant_id``, so ``AuditLog.tenant_id``'s ``default_factory``
    stamps the active tenant. That is deliberate and consistent: the purge itself runs on this
    tenant-scoped run session (see ``scheduler._run`` -> ``use_tenant``), and under RLS the
    ``DELETE`` only removes THIS tenant's audit rows — so a tenant-scoped audit entry matches
    exactly what was deleted. ``audit.record`` swallows its own errors, so it can never flip
    the purge outcome. Both the delete and this entry are committed together by the runner's
    single final commit (``purge_older_than`` no longer commits).
    """
    days = int(settings.get("audit.retention_days") or 0)
    removed = await audit_repo.purge_older_than(session, days=days)
    if not removed:
        return []
    await audit.record(
        session,
        action=audit.AUDIT_PURGED,
        actor_type="system",
        target="audit.retention_days",
        detail={"removed": removed, "retention_days": days},
    )
    return [{"step": "audit_purge", "removed": removed}]


async def _resolve_excluded_ids(session: Any, settings: dict[str, Any]) -> set[str]:
    """Exclusion ids from the rules (user values + transitive group members)."""
    excluded: set[str] = set(await exclusion_repo.user_values(session))
    group_ids = await exclusion_repo.group_ids(session)
    # Group-based exclusions need Graph. If Graph is NOT configured, don't even build the
    # client -- otherwise the MSAL authority validation already raises during construction
    # (empty `graph.tenant_id` -> `https://login.microsoftonline.com/` with no tenant
    # segment), and OUTSIDE the `try` below at that, so the raw MSAL error leaks into the
    # run (same cause as with the gated `sync_users`). Without Graph, the user-value
    # exclusions remain.
    if group_ids and is_graph_configured(settings):
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
    session_factory: Callable[[], AsyncSession],
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
        # Pre-assigned so the finally also applies if the run fails earlier.
        sender: Any = None

        try:
            # 1) Graph-Sync
            try:
                stats = await sync_users(session, settings)
                if stats.get("skipped") == "graph_not_configured":
                    # No MSAL token attempt without Graph configuration -- not an error,
                    # the run stays "success". This is made visible exclusively via this
                    # `detail_log` entry, NOT additionally as `run.error` (otherwise the
                    # message would appear twice).
                    detail.append({"step": "sync", "skipped": "graph_not_configured"})
                    log.info("run_sync_skipped", reason="graph_not_configured")
                else:
                    detail.append({"step": "sync", "checked": stats["checked"]})
            except Exception as exc:
                status = "partial"
                error = f"Sync-Fehler: {exc}"
                detail.append({"step": "sync", "error": str(exc)})
                log.error("run_sync_failed", error=str(exc))

            # 1b) Reconcile SSO users with the admin group (best effort).
            # IMPORTANT: `app_user` is instance-wide (no tenant_id RLS) -- therefore
            # deliberately runs on its own owner session, NOT on this run's tenant-scoped
            # `session` (the settings nonetheless remain those of the active tenant, read
            # from `settings` above).
            # Security fix: `sync_sso_users` needs the active tenant to strictly scope both
            # creation AND removal to this customer (otherwise it would instance-wide see
            # other customers' SSO accounts too as "no longer in the group" and delete
            # them).
            # The tenant MUST be read here, BEFORE `use_owner_context()` -- switching into
            # the owner context sets the ContextVar to `None` for the duration of the
            # block.
            tenant_id_for_sso = current_tenant_or_none()
            if tenant_id_for_sso is None:
                # No active tenant (e.g. owner/single-tenant boot path) -- without a
                # tenant the sync cannot be safely scoped, so skip instead of guessing.
                log.warning("sso_sync_skipped", reason="no_active_tenant")
            else:
                try:
                    from . import oidc

                    with use_owner_context():
                        async with session_factory() as owner_session:
                            sso_stats = await oidc.sync_sso_users(
                                owner_session, settings, tenant_id=tenant_id_for_sso
                            )
                    if sso_stats.get("removal_blocked") or sso_stats.get("admin_protected"):
                        # Make it visible: a blocked reconcile means the group
                        # configuration is wrong; a protected last admin (L-03) means a
                        # tenant nearly lost its only admin. The run must not report
                        # "success" in that case, or it would go unnoticed -- "partial"
                        # additionally triggers the admin alert.
                        status = "partial"
                        detail.append({"step": "sso_sync", **sso_stats})
                    elif sso_stats["synced"] or sso_stats["removed"]:
                        detail.append({"step": "sso_sync", **sso_stats})
                except Exception as exc:
                    detail.append({"step": "sso_sync", "error": str(exc)})
                    log.warning("sso_sync_failed", error=str(exc))

            # 1c) Apply retention periods (all off by default).
            try:
                detail.extend(await _apply_audit_retention(session, settings))
            except Exception as exc:
                log.warning("audit_purge_failed", error=str(exc))

            # Only remove departed accounts if the sync OR the retention period allows it.
            # Important: after a failed sync, all entries age simultaneously -- nothing may
            # be deleted here in that case, or an outage would wipe out the dataset.
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
            # Test mode (sync.test_mode) also notifies disabled + unlicensed accounts with
            # REAL sends. Threaded into the query here so the due-estimate below reasons over
            # the exact same candidate set as the send loop (the mass-send guard depends on
            # that consistency).
            test_mode = bool(settings.get("sync.test_mode"))
            users = await entra_repo.iter_active_for_notification(
                session, include_inactive=test_mode
            )
            checked = len(users)

            # Before the first send, estimate how many emails would be due. Deliberately
            # without the dedup query (one DB query per user) -- as an upper bound this is
            # sufficient, and exactly the failure case "suddenly everything is due" is
            # caught this way before the first email goes out.
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
                max_count=int(settings.get("schedule.max_notify_count") or 0),
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
            # Release pooled mail-sending connections -- otherwise they stay open beyond
            # the run. Also on the error path.
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
        # Admin digest / error alert (best effort -- never affects the run).
        try:
            await alerts.maybe_send_run_alert(session, settings, run, base_url)
        except Exception as exc:
            log.warning("run_alert_failed", error=str(exc))
        return run
