"""Versand-Historie + Retry fehlgeschlagener Mails."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request

from ...core.config import get_settings
from ...core.errors import NotFoundError
from ...models._base import utcnow
from ...repositories import entra_repo, notification_repo
from ...schemas.common import Message, Page
from ...schemas.entities import NotificationOut
from ...services import audit
from ...services.mail import build_sender
from ...services.settings_service import effective_base_url
from ...services.templating import build_context, email_logo, render
from ..deps import (
    AdminUser,
    CurrentUser,
    TenantSessionDep,
    TenantWriteSessionDep,
    TenantWriteSettingsDep,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _parse_day(value: str | None) -> dt.datetime | None:
    """`YYYY-MM-DD` -> Tagesbeginn (UTC). Unlesbares Datum wird ignoriert, nicht zum Fehler."""
    if not value:
        return None
    try:
        d = dt.date.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)


@router.get("", response_model=Page[NotificationOut])
async def list_notifications(
    _: CurrentUser,
    session: TenantSessionDep,
    status: str | None = None,
    user_id: int | None = None,
    search: str | None = None,
    # Datumsbereich als YYYY-MM-DD (inklusive from, exklusive Tag nach to).
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Page[NotificationOut]:
    since = _parse_day(date_from)
    until = _parse_day(date_to)
    if until is not None:
        until = until + dt.timedelta(days=1)  # bis Ende des gewählten Tages
    rows, total = await notification_repo.list_logs(
        session,
        status=status,
        entra_user_id=user_id,
        search=(search or "").strip() or None,
        since=since,
        until=until,
        page=page,
        page_size=page_size,
    )
    return Page[NotificationOut](
        items=[NotificationOut.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{log_id}/retry", response_model=Message)
async def retry(
    request: Request,
    admin: AdminUser,
    log_id: int,
    session: TenantWriteSessionDep,
    svc: TenantWriteSettingsDep,
) -> Message:
    log_entry = await notification_repo.get(session, log_id)
    if log_entry is None:
        raise NotFoundError("Log-Eintrag nicht gefunden.", code="log_not_found")
    user = await entra_repo.get(session, log_entry.entra_user_id)
    if user is None:
        raise NotFoundError("Zugehöriger Benutzer nicht gefunden.", code="user_not_found")

    settings = await svc.get_all()
    sender = build_sender(settings)
    locale = log_entry.language
    logo_url, inline_images = email_logo(
        settings, effective_base_url(settings), get_settings().static_dir
    )
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

    data = {
        "entra_user_id": log_entry.entra_user_id,
        "run_id": log_entry.run_id,
        "reminder_day": log_entry.reminder_day,
        "expiry_cycle": log_entry.expiry_cycle,
        "channel": log_entry.channel,
        "backend": sender.backend,
        "recipient": log_entry.recipient,
        "language": locale,
        "created_at": utcnow(),
    }
    # Jede Adresse als eigene Mail (zuverlässigere Zustellung, s. Notifier).
    errors: list[str] = []
    sent_any = False
    for addr in [a.strip() for a in log_entry.recipient.split(",") if a.strip()]:
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

    err_text = "; ".join(errors)[:2000] or None
    if not sent_any:
        await notification_repo.record(session, {**data, "status": "failed", "error": err_text})
        await audit.record(
            session,
            action=audit.NOTIFICATION_RETRIED,
            actor=admin,
            request=request,
            target=user.upn,
            detail={"outcome": "failed"},
        )
        await session.commit()
        raise NotFoundError(err_text or "", code="resend_failed")

    await notification_repo.record(session, {**data, "status": "sent", "error": err_text})
    await audit.record(
        session,
        action=audit.NOTIFICATION_RETRIED,
        actor=admin,
        request=request,
        target=user.upn,
        detail={"outcome": "sent"},
    )
    await session.commit()
    return Message(message="Erneut gesendet.")
