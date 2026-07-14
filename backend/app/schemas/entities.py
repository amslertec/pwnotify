"""Ausgabe-Schemas für Entra-User, Notifications, Runs, Exclusions."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict


class EntraUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entra_id: str
    upn: str
    display_name: str
    mail: str | None
    other_mails: list[str]
    account_enabled: bool
    department: str | None
    job_title: str | None
    language: str | None
    last_password_change: dt.datetime | None
    password_policies: str | None
    password_never_expires: bool
    expiry_date: dt.datetime | None
    days_left: int | None
    excluded: bool
    is_shared: bool
    last_synced_at: dt.datetime


class EntraUserDetail(EntraUserOut):
    raw: dict[str, Any]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entra_user_id: int
    run_id: int | None
    reminder_day: int
    expiry_cycle: str
    channel: str
    backend: str
    recipient: str
    language: str
    status: str
    error: str | None
    created_at: dt.datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    dry_run: bool
    started_at: dt.datetime
    finished_at: dt.datetime | None
    duration_ms: int | None
    checked_users: int
    sent: int
    failed: int
    skipped: int
    error: str | None


class RunDetail(RunOut):
    detail_log: list[dict[str, Any]]


class ExclusionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    value: str
    label: str | None
    created_at: dt.datetime
