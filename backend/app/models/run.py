"""Protokoll der Scheduler-Läufe."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ._base import utcnow


class Run(SQLModel, table=True):
    __tablename__ = "run"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, nullable=False)
    trigger: str = Field(
        default="schedule", sa_column=Column(String(16), nullable=False)
    )  # schedule|manual
    status: str = Field(
        default="running", sa_column=Column(String(16), nullable=False)
    )  # running|success|partial|error
    dry_run: bool = Field(default=False)

    started_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    finished_at: dt.datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    duration_ms: int | None = Field(default=None)

    checked_users: int = Field(default=0)
    sent: int = Field(default=0)
    failed: int = Field(default=0)
    skipped: int = Field(default=0)

    error: str | None = Field(default=None, sa_column=Column(String(2000)))
    detail_log: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
