"""Admin-Benachrichtigungen: Digest nach geplantem Lauf + sofortiger Fehler-Alert.

Geht an eine konfigurierbare Empfängerliste (``alerts.recipients``) über das aktive
Mail-Backend. Fehler beim Versand dürfen den Lauf nie beeinflussen (best effort).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.logging import get_logger
from ..models.run import Run
from ..repositories import entra_repo
from .mail import build_sender
from .secret_expiry import SecretExpiry
from .secret_expiry import check as check_secret_expiry
from .templating import email_logo

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


def _row(label: str, value: object, *, accent: str | None = None) -> str:
    color = accent or "#0f172a"
    return (
        f'<tr><td style="padding:6px 16px 6px 0;color:#64748b;font-size:14px">{label}</td>'
        f'<td style="padding:6px 0;font-weight:600;font-size:14px;color:{color};'
        f'text-align:right">{value}</td></tr>'
    )


def _render(
    lang: str,
    run: Run,
    counts: dict[str, int],
    base_url: str,
    failure: bool,
    secret: SecretExpiry | None = None,
    *,
    logo_url: str = "",
    app_name: str = "PwNotify",
    company_name: str = "",
) -> tuple[str, str, str]:
    """Digest/Alert im selben Karten-Layout wie die Erinnerungs-Mails (Logo, Rundung)."""
    t = _T.get(lang, _T["de"])
    subject = (t["subject_fail"] if failure else t["subject_ok"]).format(sent=run.sent)
    dur = f"{(run.duration_ms or 0) / 1000:.1f} s"
    accent = "#dc2626" if failure else "#4F46E5"
    fail_color = "#dc2626" if run.failed else None

    rows = "".join(
        [
            _row(t["status"], run.status),
            _row(t["checked"], run.checked_users),
            _row(t["sent"], run.sent),
            _row(t["failed"], run.failed, accent=fail_color),
            _row(t["skipped"], run.skipped),
            _row(t["duration"], dur),
        ]
    )
    if run.error:
        rows += _row(t["error"], run.error, accent="#dc2626")
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
            f'<p style="background:#fef2f2;border-left:3px solid #dc2626;padding:12px 14px;'
            f'margin:0 0 18px;border-radius:8px;font-size:14px;color:#7f1d1d;line-height:1.5">'
            f"{msg}</p>"
        )
        hinweis_text = f"{msg}\n\n"

    logo_img = (
        f'<img src="{logo_url}" alt="{app_name}" '
        f'style="height:28px;width:auto;border:0;display:block">'
        if logo_url
        else f'<div style="font-weight:700;font-size:18px;color:#0f172a">{app_name}</div>'
    )
    table_style = "border-collapse:collapse;width:100%"

    html = (
        '<div style="width:100%;background:#eef1f6;padding:24px 0;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:16px;'
        'overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,.08)">'
        f'<div style="padding:26px 32px 0">{logo_img}</div>'
        '<div style="padding:14px 32px 8px;color:#0f172a">'
        f"{hinweis_html}"
        f'<h1 style="font-size:20px;margin:0 0 4px;color:{accent}">{t["heading"]}</h1>'
        f'<table style="{table_style};margin-top:8px">{rows}</table>'
        f'<h2 style="font-size:15px;margin:22px 0 4px;color:#0f172a">{t["overview"]}</h2>'
        f'<table style="{table_style}">{over}</table>'
        f'<p style="margin:22px 0 4px"><a href="{base_url}" '
        f'style="display:inline-block;background:{accent};color:#ffffff;padding:12px 22px;'
        f'border-radius:10px;text-decoration:none;font-weight:600;font-size:14px">'
        f"{t['open']}</a></p></div>"
        f'<div style="padding:18px 32px 28px;font-size:12px;color:#94a3b8">'
        f"{company_name or app_name} · {t['footer']}</div>"
        "</div></div>"
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
    # Gleiches eingebettetes Logo wie in den Erinnerungs-Mails (Inline-CID, kein Netz nötig).
    logo_url, inline_images = email_logo(settings, base_url, get_settings().static_dir)
    subject, html, text = _render(
        lang,
        run,
        counts,
        base_url,
        is_failure,
        secret if secret and secret.should_alert else None,
        logo_url=logo_url,
        app_name=str(settings.get("branding.app_name") or "PwNotify"),
        company_name=str(settings.get("branding.company_name") or ""),
    )
    sender = build_sender(settings)
    for addr in recipients:
        try:
            await sender.send(
                to=[addr],
                subject=subject,
                html_body=html,
                text_body=text,
                inline_images=inline_images,
            )
        except Exception as exc:  # Versandfehler dürfen den Lauf nicht beeinflussen
            log.warning("alert_send_failed", to=addr, error=str(exc))
    log.info("run_alert_sent", recipients=len(recipients), failure=is_failure)
