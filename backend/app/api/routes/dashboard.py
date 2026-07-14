"""Dashboard-Aggregation."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter

from ...repositories import entra_repo, notification_repo, run_repo
from ...schemas.entities import EntraUserOut, RunOut
from ...services.scheduler import get_scheduler
from ..deps import CurrentUser, SessionDep, SettingsDep

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def dashboard(_: CurrentUser, session: SessionDep, svc: SettingsDep) -> dict[str, Any]:
    counts = await entra_repo.counts_for_dashboard(session)

    today_start = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    mails_today = await notification_repo.count_sent_since(session, today_start)

    histogram = await entra_repo.expiry_histogram(session, days=30)
    top_rows, _total = await entra_repo.list_users(
        session, sort_by="days_left", sort_dir="asc", page=1, page_size=10
    )
    top = [
        EntraUserOut.model_validate(r, from_attributes=True)
        for r in top_rows
        if r.days_left is not None
    ]

    last_run = await run_repo.latest(session)
    settings = await svc.get_all()
    graph_configured = bool(
        settings.get("graph.tenant_id")
        and settings.get("graph.client_id")
        and settings.get("graph.client_secret")
    )
    mail_configured = bool(settings.get("mail.from"))

    try:
        next_run = get_scheduler().next_run_time()
    except RuntimeError:
        next_run = None

    return {
        "kpis": {
            "total": counts["total"],
            "expiring_soon": counts["expiring_soon"],
            "expired": counts["expired"],
            "never": counts["never"],
            "disabled": counts["disabled"],
            "mails_today": mails_today,
        },
        "status_distribution": [
            {
                "status": "ok",
                "count": counts["total"]
                - counts["expiring_soon"]
                - counts["expired"]
                - counts["never"]
                - counts["disabled"],
            },
            {"status": "soon", "count": counts["expiring_soon"]},
            {"status": "expired", "count": counts["expired"]},
            {"status": "never", "count": counts["never"]},
            {"status": "disabled", "count": counts["disabled"]},
        ],
        "expiry_histogram": histogram,
        "top_upcoming": [t.model_dump(mode="json") for t in top],
        "last_run": RunOut.model_validate(last_run, from_attributes=True).model_dump(mode="json")
        if last_run
        else None,
        "next_run": next_run.isoformat() if next_run else None,
        "backends": {
            "graph_configured": graph_configured,
            "mail_configured": mail_configured,
            "mail_backend": settings.get("mail.backend", "graph"),
        },
    }
