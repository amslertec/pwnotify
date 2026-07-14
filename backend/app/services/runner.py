"""Orchestriert einen kompletten Lauf: Graph-Sync -> Benachrichtigungen -> Run-Protokoll.

Fehler einzelner User oder eines Teilschritts dürfen den Lauf nie abbrechen.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.logging import get_logger
from ..models.run import Run
from ..repositories import entra_repo, exclusion_repo, run_repo
from . import alerts
from .graph import GraphClient, GraphConfig
from .graph.sync import sync_users
from .mail import build_sender
from .notifier import notify_user
from .settings_service import SettingsService, effective_base_url

log = get_logger("runner")


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

        try:
            # 1) Graph-Sync
            try:
                stats = await sync_users(session, settings)
                detail.append({"step": "sync", "checked": stats["checked"]})
            except Exception as exc:
                status = "partial"
                error = f"Sync-Fehler: {exc}"
                detail.append({"step": "sync", "error": str(exc)})
                log.error("run_sync_failed", error=str(exc))

            # 1b) SSO-Benutzer mit der Admin-Gruppe abgleichen (best effort)
            try:
                from . import oidc

                sso_stats = await oidc.sync_sso_users(session, settings)
                if sso_stats["synced"] or sso_stats["removed"]:
                    detail.append({"step": "sso_sync", **sso_stats})
            except Exception as exc:
                detail.append({"step": "sso_sync", "error": str(exc)})
                log.warning("sso_sync_failed", error=str(exc))

            # 2) Benachrichtigungen
            excluded_ids = await _resolve_excluded_ids(session, settings)
            sender = build_sender(settings)
            users = await entra_repo.iter_active_for_notification(session)
            checked = len(users)

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
