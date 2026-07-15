"""Admin-Benachrichtigungen: Digest nach geplantem Lauf + sofortiger Fehler-Alert.

Geht an eine konfigurierbare Empfängerliste (``alerts.recipients``) über das aktive
Mail-Backend. Fehler beim Versand dürfen den Lauf nie beeinflussen (best effort).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..models.run import Run
from ..repositories import entra_repo
from .mail import build_sender
from .secret_expiry import SecretExpiry
from .secret_expiry import check as check_secret_expiry

log = get_logger("alerts")

_T: dict[str, dict[str, str]] = {
    "de": {
        "subject_ok": "PwNotify: Lauf abgeschlossen — {sent} gesendet",
        "subject_fail": "PwNotify: Lauf mit Fehlern — bitte prüfen",
        "heading": "Lauf-Zusammenfassung",
        "status": "Status",
        "checked": "Geprüft",
        "sent": "Gesendet",
        "failed": "Fehlgeschlagen",
        "skipped": "Übersprungen",
        "duration": "Dauer",
        "error": "Fehler",
        "overview": "Benutzer-Übersicht",
        "total": "Gesamt",
        "soon": "Läuft in ≤ 7 Tagen ab",
        "expired": "Abgelaufen",
        "open": "Im Dashboard öffnen",
        "footer": "Automatische Benachrichtigung von PwNotify.",
        "secret_soon": (
            "Achtung: Das Graph-Client-Secret läuft in {days} Tagen ab ({date}). "
            "Danach schlägt der Sync fehl und es gehen keine Erinnerungen mehr raus. "
            "Bitte rechtzeitig ein neues Secret hinterlegen."
        ),
        "secret_expired": (
            "Achtung: Das Graph-Client-Secret ist am {date} abgelaufen. Der Sync "
            "schlägt fehl, bis ein neues Secret hinterlegt wird."
        ),
    },
    "en": {
        "subject_ok": "PwNotify: run completed — {sent} sent",
        "subject_fail": "PwNotify: run finished with errors — please check",
        "heading": "Run summary",
        "status": "Status",
        "checked": "Checked",
        "sent": "Sent",
        "failed": "Failed",
        "skipped": "Skipped",
        "duration": "Duration",
        "error": "Error",
        "overview": "User overview",
        "total": "Total",
        "soon": "Expiring in ≤ 7 days",
        "expired": "Expired",
        "open": "Open in dashboard",
        "footer": "Automated notification from PwNotify.",
        "secret_soon": (
            "Heads-up: the Graph client secret expires in {days} days ({date}). "
            "After that the sync fails and no reminders go out. "
            "Please store a new secret in time."
        ),
        "secret_expired": (
            "Heads-up: the Graph client secret expired on {date}. The sync will keep "
            "failing until a new secret is stored."
        ),
    },
}


def _row(label: str, value: object) -> str:
    return (
        f'<tr><td style="padding:4px 12px 4px 0;color:#64748b">{label}</td>'
        f'<td style="padding:4px 0;font-weight:600">{value}</td></tr>'
    )


def _render(
    lang: str,
    run: Run,
    counts: dict[str, int],
    base_url: str,
    failure: bool,
    secret: SecretExpiry | None = None,
) -> tuple[str, str, str]:
    t = _T.get(lang, _T["de"])
    subject = (t["subject_fail"] if failure else t["subject_ok"]).format(sent=run.sent)
    dur = f"{(run.duration_ms or 0) / 1000:.1f} s"
    accent = "#dc2626" if failure else "#4F46E5"

    rows = "".join(
        [
            _row(t["status"], run.status),
            _row(t["checked"], run.checked_users),
            _row(t["sent"], run.sent),
            _row(t["failed"], run.failed),
            _row(t["skipped"], run.skipped),
            _row(t["duration"], dur),
        ]
    )
    if run.error:
        rows += _row(t["error"], run.error)
    over = "".join(
        [
            _row(t["total"], counts.get("total", 0)),
            _row(t["soon"], counts.get("expiring_soon", 0)),
            _row(t["expired"], counts.get("expired", 0)),
        ]
    )
    # Ein ablaufender Zugang gehört ganz nach oben: Er kündigt einen Totalausfall an, der
    # sonst niemandem auffiele — ausbleibende Erinnerungen bemerkt keiner.
    hinweis_html = hinweis_text = ""
    if secret is not None:
        msg = (
            t["secret_expired"].format(date=secret.expires_at.isoformat())
            if secret.expired
            else t["secret_soon"].format(days=secret.days_left, date=secret.expires_at.isoformat())
        )
        hinweis_html = (
            f'<p style="background:#fef2f2;border-left:3px solid #dc2626;padding:10px 12px;'
            f'margin:0 0 16px;font-size:14px;color:#7f1d1d">{msg}</p>'
        )
        hinweis_text = f"{msg}\n\n"

    html = (
        f'<div style="font-family:system-ui,Segoe UI,sans-serif;max-width:520px;margin:0 auto;'
        f'color:#0f172a">'
        f"{hinweis_html}"
        f'<h2 style="color:{accent};font-size:18px;margin:0 0 12px">{t["heading"]}</h2>'
        f'<table style="border-collapse:collapse;font-size:14px">{rows}</table>'
        f'<h3 style="font-size:15px;margin:20px 0 8px">{t["overview"]}</h3>'
        f'<table style="border-collapse:collapse;font-size:14px">{over}</table>'
        f'<p style="margin:20px 0"><a href="{base_url}" '
        f'style="background:{accent};color:#fff;padding:9px 16px;border-radius:8px;'
        f'text-decoration:none;font-size:14px">{t["open"]}</a></p>'
        f'<p style="color:#94a3b8;font-size:12px">{t["footer"]}</p></div>'
    )
    text = (
        hinweis_text + f"{t['heading']}\n"
        f"{t['status']}: {run.status}\n{t['checked']}: {run.checked_users}\n"
        f"{t['sent']}: {run.sent}\n{t['failed']}: {run.failed}\n{t['skipped']}: {run.skipped}\n"
        f"{t['duration']}: {dur}\n"
        + (f"{t['error']}: {run.error}\n" if run.error else "")
        + f"\n{t['overview']}\n{t['total']}: {counts.get('total', 0)}\n"
        f"{t['soon']}: {counts.get('expiring_soon', 0)}\n"
        f"{t['expired']}: {counts.get('expired', 0)}\n"
        f"\n{base_url}\n"
    )
    return subject, html, text


async def maybe_send_run_alert(
    session: AsyncSession, settings: dict[str, Any], run: Run, base_url: str
) -> None:
    if not bool(settings.get("alerts.enabled")):
        return
    recipients = [
        r.strip()
        for r in (settings.get("alerts.recipients") or [])
        if isinstance(r, str) and r.strip()
    ]
    if not recipients:
        return
    is_failure = run.status in ("partial", "error")
    want_digest = bool(settings.get("alerts.digest")) and run.trigger == "schedule"
    want_alert = bool(settings.get("alerts.on_failure")) and is_failure
    if not (want_digest or want_alert):
        return

    lang = "en" if settings.get("template.language_default") == "en" else "de"
    counts = await entra_repo.counts_for_dashboard(session)
    secret = check_secret_expiry(str(settings.get("graph.client_secret_expires_at") or ""))
    subject, html, text = _render(
        lang, run, counts, base_url, is_failure, secret if secret and secret.should_alert else None
    )
    sender = build_sender(settings)
    for addr in recipients:
        try:
            await sender.send(to=[addr], subject=subject, html_body=html, text_body=text)
        except Exception as exc:  # Versandfehler dürfen den Lauf nicht beeinflussen
            log.warning("alert_send_failed", to=addr, error=str(exc))
    log.info("run_alert_sent", recipients=len(recipients), failure=is_failure)
