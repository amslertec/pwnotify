"""Benachrichtigungslogik: Reminder-Stufe bestimmen, dedupen, versenden."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.logging import get_logger
from ..models.entra import EntraUser
from ..repositories import notification_repo
from .expiry import due_reminder_stage
from .mail.base import MailSender
from .recipients import resolve_recipients
from .templating import build_context, email_logo, render

log = get_logger("notifier")


@dataclass
class NotifyOutcome:
    action: str  # sent | dry_run | skipped | failed
    stage: int | None = None
    recipient: str | None = None
    channel: str | None = None
    reason: str | None = None
    error: str | None = None


def _normalize_locale(value: str | None, default: str) -> str:
    v = (value or default or "de").lower()
    return "en" if v.startswith("en") else "de"


async def notify_user(
    session: AsyncSession,
    user: EntraUser,
    *,
    settings: dict[str, Any],
    sender: MailSender,
    base_url: str,
    reminder_days: list[int],
    excluded_ids: set[str],
    dry_run: bool,
    run_id: int | None,
    force: bool = False,
    now: dt.datetime | None = None,
) -> NotifyOutcome:
    if not force and (user.entra_id in excluded_ids or user.upn in excluded_ids):
        return NotifyOutcome(action="skipped", reason="excluded_rule")
    if user.expiry_date is None or user.days_left is None:
        return NotifyOutcome(action="skipped", reason="no_expiry")

    cycle = user.expiry_date.date().isoformat()
    stage: int | None
    if force:
        # Manueller Sofort-Versand: aktuelle Stufe unabhängig vom Dedup wählen.
        eligible = [d for d in reminder_days if user.days_left <= d]
        stage = min(eligible) if eligible else (max(reminder_days) if reminder_days else 0)
    else:
        already = await notification_repo.sent_stages(session, user.id, cycle)  # type: ignore[arg-type]
        stage = due_reminder_stage(
            days_left=user.days_left, reminder_days=reminder_days, already_sent=already
        )
    if stage is None:
        return NotifyOutcome(action="skipped", reason="not_due")

    strategy = settings.get("mail.recipient_strategy", "primary")
    recipients, channel = resolve_recipients(
        strategy,
        user.mail,
        user.other_mails,
        upn=user.upn,
        upn_fallback=bool(settings.get("mail.upn_fallback")),
    )
    if not recipients:
        return NotifyOutcome(action="skipped", stage=stage, reason="no_recipient")

    per_user = settings.get("template.language_per_user", True)
    default_lang = settings.get("template.language_default", "de")
    locale = _normalize_locale(user.language if per_user else None, default_lang)

    logo_url, inline_images = email_logo(settings, base_url, get_settings().static_dir)
    context = build_context(
        display_name=user.display_name,
        upn=user.upn,
        days_left=user.days_left,
        expiry_date=user.expiry_date,
        reset_url=settings.get("branding.reset_url") or "",
        company_name=settings.get("branding.company_name") or "",
        app_name=settings.get("branding.app_name") or "PwNotify",
        logo_url=logo_url,
        primary_color=settings.get("branding.primary_color") or "#4F46E5",
        locale=locale,
    )
    subject = render(settings[f"template.subject_{locale}"], context, html=False)
    html_body = render(settings[f"template.html_{locale}"], context, html=True)
    text_body = render(settings[f"template.text_{locale}"], context, html=False)
    recipient_str = ", ".join(recipients)

    if dry_run:
        return NotifyOutcome(
            action="dry_run", stage=stage, recipient=recipient_str, channel=channel
        )

    now = now or dt.datetime.now(dt.UTC)
    base_log: dict[str, Any] = {
        "entra_user_id": user.id,
        "run_id": run_id,
        "reminder_day": stage,
        "expiry_cycle": cycle,
        "channel": channel,
        "backend": sender.backend,
        "recipient": recipient_str,
        "language": locale,
        "created_at": now,
    }
    # Jede Adresse als EIGENE Mail versenden. Eine gemischte Mail an interne + externe
    # Empfänger wird von Exchange sonst oft nur teilweise zugestellt (interne Kopie
    # gefiltert). So verhält sich jede Adresse wie ein einzelner Empfänger.
    errors: list[str] = []
    sent_any = False
    for addr in recipients:
        try:
            await sender.send(
                to=[addr],
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                inline_images=inline_images,
            )
            sent_any = True
        except Exception as exc:
            errors.append(f"{addr}: {exc}"[:400])
            log.warning("notify_addr_failed", upn=user.upn, addr=addr, error=str(exc))

    err_text = "; ".join(errors)[:2000] or None
    if not sent_any:
        await notification_repo.record(session, {**base_log, "status": "failed", "error": err_text})
        await session.commit()
        return NotifyOutcome(
            action="failed", stage=stage, recipient=recipient_str, channel=channel, error=err_text
        )

    await notification_repo.record(session, {**base_log, "status": "sent", "error": err_text})
    await session.commit()
    return NotifyOutcome(action="sent", stage=stage, recipient=recipient_str, channel=channel)
