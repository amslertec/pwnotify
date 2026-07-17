"""Entra-User: Liste, Detail, Exclude, Sofort-Reminder, Export, Bulk."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...core.config import get_settings
from ...core.errors import NotFoundError, PwNotifyError
from ...repositories import entra_repo
from ...schemas.common import Message, Page
from ...schemas.entities import EntraUserDetail, EntraUserOut
from ...services.mail import build_sender
from ...services.notifier import notify_user
from ..deps import AdminUser, CurrentUser, TenantSessionDep, TenantSettingsDep

router = APIRouter(prefix="/users", tags=["users"])

# Obergrenze fuer einen Export. Darueber wird abgelehnt statt abgeschnitten —
# ein unvollstaendiger Export, der vollstaendig aussieht, ist gefaehrlicher.
_EXPORT_MAX_ROWS = 100_000


class ExcludeRequest(BaseModel):
    excluded: bool


class BulkRequest(BaseModel):
    ids: list[int]
    action: str  # exclude | include | notify


@router.get("", response_model=Page[EntraUserOut])
async def list_users(
    _: CurrentUser,
    session: TenantSessionDep,
    search: str | None = None,
    status: str | None = None,
    sort_by: str = "days_left",
    sort_dir: str = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> Page[EntraUserOut]:
    rows, total = await entra_repo.list_users(
        session,
        search=search,
        status=status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    return Page[EntraUserOut](
        items=[EntraUserOut.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/export")
async def export_users(
    _: CurrentUser,
    session: TenantSessionDep,
    fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: str | None = None,
    status: str | None = None,
) -> StreamingResponse:
    rows, total = await entra_repo.list_users(
        session, search=search, status=status, page=1, page_size=_EXPORT_MAX_ROWS
    )
    if total > _EXPORT_MAX_ROWS:
        # Nicht stillschweigend abschneiden: Ein Export, der so aussieht wie ein voller,
        # aber Zeilen unterschlägt, ist schlimmer als eine klare Fehlermeldung.
        raise PwNotifyError(
            f"Der Export umfasst {total} Benutzer, das Maximum sind {_EXPORT_MAX_ROWS}. "
            "Bitte über Suche oder Status filtern.",
            code="export_too_large",
        )
    headers = [
        "displayName",
        "upn",
        "mail",
        "otherMails",
        "department",
        "jobTitle",
        "lastPasswordChange",
        "expiryDate",
        "daysLeft",
        "accountEnabled",
        "neverExpires",
        "excluded",
    ]

    def row_values(u: Any) -> list[Any]:
        return [
            u.display_name,
            u.upn,
            u.mail or "",
            ";".join(u.other_mails or []),
            u.department or "",
            u.job_title or "",
            u.last_password_change.isoformat() if u.last_password_change else "",
            u.expiry_date.isoformat() if u.expiry_date else "",
            u.days_left if u.days_left is not None else "",
            u.account_enabled,
            u.password_never_expires,
            u.excluded,
        ]

    # Aufbereitung im Thread: openpyxl und csv sind reine CPU-Arbeit ohne I/O und würden
    # den Event-Loop blockieren. Bei `workers=1` (der Scheduler läuft im selben Prozess)
    # steht in der Zeit alles andere still — gemessen ~0,3 s je 10.000 Zeilen.
    werte = [row_values(u) for u in rows]

    if fmt == "xlsx":
        buf = await asyncio.to_thread(_build_xlsx, headers, werte)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=pwnotify-users.xlsx"},
        )

    inhalt = await asyncio.to_thread(_build_csv, headers, werte)
    return StreamingResponse(
        iter([inhalt]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pwnotify-users.csv"},
    )


def _build_xlsx(headers: list[str], werte: list[list[Any]]) -> io.BytesIO:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(headers)
    for zeile in werte:
        ws.append([str(v) if isinstance(v, bool) else v for v in zeile])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _build_csv(headers: list[str], werte: list[list[Any]]) -> str:
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(headers)
    writer.writerows(werte)
    return sio.getvalue()


@router.get("/{user_id}", response_model=EntraUserDetail)
async def get_user(_: CurrentUser, user_id: int, session: TenantSessionDep) -> EntraUserDetail:
    user = await entra_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    return EntraUserDetail.model_validate(user, from_attributes=True)


@router.post("/{user_id}/exclude", response_model=Message)
async def set_exclude(
    _: AdminUser, user_id: int, body: ExcludeRequest, session: TenantSessionDep
) -> Message:
    user = await entra_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    await entra_repo.set_excluded(session, user_id, body.excluded)
    return Message(message="Aktualisiert.")


@router.post("/{user_id}/notify", response_model=Message)
async def notify_now(
    _: AdminUser, user_id: int, session: TenantSessionDep, svc: TenantSettingsDep
) -> Message:
    user = await entra_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    settings = await svc.get_all()
    sender = build_sender(settings)
    outcome = await notify_user(
        session,
        user,
        settings=settings,
        sender=sender,
        base_url=get_settings().base_url,
        reminder_days=settings.get("schedule.reminder_days") or [14, 7, 3, 1, 0],
        excluded_ids=set(),
        dry_run=False,
        run_id=None,
        force=True,
    )
    if outcome.action == "failed":
        raise NotFoundError(outcome.error or "", code="send_failed")
    if outcome.action == "skipped":
        return Message(message=f"Kein Versand: {outcome.reason}")
    return Message(message=f"Reminder an {outcome.recipient} gesendet.")


@router.post("/bulk", response_model=Message)
async def bulk(
    _: AdminUser, body: BulkRequest, session: TenantSessionDep, svc: TenantSettingsDep
) -> Message:
    if body.action in ("exclude", "include"):
        for uid in body.ids:
            await entra_repo.set_excluded(session, uid, body.action == "exclude")
        return Message(message=f"{len(body.ids)} Benutzer aktualisiert.")
    if body.action == "notify":
        settings = await svc.get_all()
        sender = build_sender(settings)
        sent = 0
        for uid in body.ids:
            user = await entra_repo.get(session, uid)
            if user is None:
                continue
            outcome = await notify_user(
                session,
                user,
                settings=settings,
                sender=sender,
                base_url=get_settings().base_url,
                reminder_days=settings.get("schedule.reminder_days") or [14, 7, 3, 1, 0],
                excluded_ids=set(),
                dry_run=False,
                run_id=None,
                force=True,
            )
            if outcome.action == "sent":
                sent += 1
        return Message(message=f"{sent} Reminder gesendet.")
    raise NotFoundError("Unbekannte Aktion.", code="unknown_action")
