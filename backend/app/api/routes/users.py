"""Entra users: list, detail, exclude, immediate reminder, export, bulk."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.errors import NotFoundError, PwNotifyError
from ...core.logging import get_logger
from ...repositories import entra_repo
from ...schemas.common import Message, Page
from ...schemas.entities import EntraUserDetail, EntraUserOut
from ...services import audit
from ...services.mail import build_sender
from ...services.notifier import notify_user
from ...services.runner import mass_send_blocked_reason
from ..deps import (
    AdminUser,
    CurrentUser,
    TenantSessionDep,
    TenantWriteSessionDep,
    TenantWriteSettingsDep,
)

router = APIRouter(prefix="/users", tags=["users"])

log = get_logger("users")

# Upper bound for a single export. Above this it is rejected rather than truncated --
# an incomplete export that looks complete is more dangerous.
_EXPORT_MAX_ROWS = 100_000


class ExcludeRequest(BaseModel):
    excluded: bool


class BulkRequest(BaseModel):
    # Hard cap the payload: an untyped `ids: list[int]` let a single request drive an
    # unbounded number of sends past the mass-send brake (finding H2). 2000 mirrors the
    # existing convention in schemas/assignment.py; a Literal forbids unknown actions.
    # Pydantic rejects a violation with 422 before any mail logic runs.
    ids: list[int] = Field(min_length=1, max_length=2000)
    action: Literal["exclude", "include", "notify"]


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
    request: Request,
    user: CurrentUser,
    session: TenantSessionDep,
    fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
    search: str | None = None,
    status: str | None = None,
) -> StreamingResponse:
    rows, total = await entra_repo.list_users(
        session, search=search, status=status, page=1, page_size=_EXPORT_MAX_ROWS
    )
    if total > _EXPORT_MAX_ROWS:
        # Do not silently truncate: an export that looks complete but drops rows
        # is worse than a clear error message.
        raise PwNotifyError(
            f"Der Export umfasst {total} Benutzer, das Maximum sind {_EXPORT_MAX_ROWS}. "
            "Bitte über Suche oder Status filtern.",
            code="export_too_large",
        )
    # Audit (finding L3): a full-tenant PII export must never be traceless. Record who
    # exported how many rows in which format -- NEVER the exported PII itself, only counts
    # and filter flags. Committed BEFORE the streaming response is handed back, because the
    # dependency-scoped session closes once the response body has been streamed. `session`
    # is tenant-scoped (runtime role, which holds CRUD on `audit_log`), so the entry is
    # stamped with the active tenant by `AuditLog.tenant_id`'s default_factory.
    await audit.record(
        session,
        action=audit.USERS_EXPORTED,
        actor=user,
        request=request,
        detail={"format": fmt, "count": total, "search": bool(search), "status": status},
    )
    await session.commit()
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

    # Build in a thread: openpyxl and csv are pure CPU work with no I/O and would
    # block the event loop. With `workers=1` (the scheduler runs in the same process),
    # everything else would stall meanwhile -- measured ~0.3s per 10,000 rows.
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


# Leading characters that make a spreadsheet app (Excel, LibreOffice, Sheets) treat a cell
# as a formula rather than text. openpyxl even persists a leading "=" as data_type='f' (a
# REAL formula), and csv.writer merely quotes such a value -- Excel still evaluates it. A tab
# or CR can push the payload into a formula context in some parsers, so both are included.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _neutralize_cell(value: Any) -> Any:
    """Prefix a leading formula trigger with an apostrophe so spreadsheet apps treat the cell
    as text, not a formula (CSV injection / CWE-1236). Only strings are at risk; numeric,
    bool and date/ISO-string cells pass through unchanged so legitimate columns (daysLeft,
    dates) stay typed and are not accidentally quoted."""
    if isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def _build_xlsx(headers: list[str], werte: list[list[Any]]) -> io.BytesIO:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(headers)
    for zeile in werte:
        # Neutralise BEFORE stringifying bools so both sinks (xlsx/csv) treat cells identically.
        ws.append([_neutralize_cell(str(v) if isinstance(v, bool) else v) for v in zeile])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _build_csv(headers: list[str], werte: list[list[Any]]) -> str:
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(headers)
    writer.writerows([_neutralize_cell(v) for v in zeile] for zeile in werte)
    return sio.getvalue()


@router.get("/{user_id}", response_model=EntraUserDetail)
async def get_user(_: CurrentUser, user_id: int, session: TenantSessionDep) -> EntraUserDetail:
    user = await entra_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    return EntraUserDetail.model_validate(user, from_attributes=True)


@router.post("/{user_id}/exclude", response_model=Message)
async def set_exclude(
    request: Request,
    admin: AdminUser,
    user_id: int,
    body: ExcludeRequest,
    session: TenantWriteSessionDep,
) -> Message:
    user = await entra_repo.get(session, user_id)
    if user is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    await entra_repo.set_excluded(session, user_id, body.excluded)
    await audit.record(
        session,
        action=audit.USER_EXCLUDED,
        actor=admin,
        request=request,
        target=user.upn,
        detail={"excluded": body.excluded, "count": 1, "kind": "single"},
    )
    await session.commit()
    return Message(message="Aktualisiert.")


@router.post("/{user_id}/notify", response_model=Message)
async def notify_now(
    request: Request,
    admin: AdminUser,
    user_id: int,
    session: TenantWriteSessionDep,
    svc: TenantWriteSettingsDep,
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
    await audit.record(
        session,
        action=audit.NOTIFICATION_SENT_MANUAL,
        actor=admin,
        request=request,
        target=user.upn,
        detail={"count": 1, "outcome": outcome.action},
    )
    await session.commit()
    if outcome.action == "failed":
        # Never surface the raw transport error (M10): SMTP/Graph text carries server banners,
        # internal hostnames and tenant GUIDs. The detail goes to the log only; the client gets
        # a generic message that points the operator at the log.
        log.warning("manual_send_failed", user=user.upn, error=outcome.error)
        raise NotFoundError("Versand fehlgeschlagen. Bitte Log prüfen.", code="send_failed")
    if outcome.action == "skipped":
        return Message(message=f"Kein Versand: {outcome.reason}")
    return Message(message=f"Reminder an {outcome.recipient} gesendet.")


@router.post("/bulk", response_model=Message)
async def bulk(
    request: Request,
    admin: AdminUser,
    body: BulkRequest,
    session: TenantWriteSessionDep,
    svc: TenantWriteSettingsDep,
) -> Message:
    if body.action in ("exclude", "include"):
        for uid in body.ids:
            await entra_repo.set_excluded(session, uid, body.action == "exclude")
        await audit.record(
            session,
            action=audit.USER_EXCLUDED,
            actor=admin,
            request=request,
            detail={"excluded": body.action == "exclude", "count": len(body.ids), "kind": "bulk"},
        )
        await session.commit()
        return Message(message=f"{len(body.ids)} Benutzer aktualisiert.")
    if body.action == "notify":
        settings = await svc.get_all()
        # Route bulk-notify through the SAME absolute ceiling that runner.execute_run
        # enforces (finding H2): without this, an admin could dispatch thousands of real
        # reminders under the customer's sender reputation, entirely past the mass-send
        # brake. Every requested id is a deliberate send, so due == checked == len(ids);
        # the ratio brake is meaningless here (would always be 100%), so it is switched off
        # (max_ratio=0.0) and only the absolute count cap decides.
        requested = len(body.ids)
        mass_block = mass_send_blocked_reason(
            due=requested,
            checked=requested,
            max_ratio=0.0,
            max_count=int(settings.get("schedule.max_notify_count") or 0),
        )
        if mass_block:
            # Nothing was sent -- record the refusal for the audit trail, then reject.
            await audit.record(
                session,
                action=audit.NOTIFICATION_SENT_MANUAL,
                actor=admin,
                request=request,
                outcome="blocked",
                detail={"blocked": mass_block, "requested": requested, "kind": "bulk"},
            )
            await session.commit()
            raise PwNotifyError(mass_block, code="mass_send_blocked")
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
        await audit.record(
            session,
            action=audit.NOTIFICATION_SENT_MANUAL,
            actor=admin,
            request=request,
            detail={"count": sent, "requested": len(body.ids), "kind": "bulk"},
        )
        await session.commit()
        return Message(message=f"{sent} Reminder gesendet.")
    raise NotFoundError("Unbekannte Aktion.", code="unknown_action")
