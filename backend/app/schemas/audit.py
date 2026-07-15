"""Schemas für das Audit-Protokoll."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel


class AuditEntryOut(BaseModel):
    id: int
    at: dt.datetime
    actor_username: str | None = None
    actor_type: str = "user"
    # Stabile Kennung wie "user.role_changed"; das Frontend übersetzt sie.
    action: str
    target: str | None = None
    outcome: str = "success"
    ip_address: str | None = None
    user_agent: str | None = None
    detail: dict[str, Any] = {}


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    total: int
    page: int
    page_size: int
