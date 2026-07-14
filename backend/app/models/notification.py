"""Versand-Protokoll mit Dedup-Constraint."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from ._base import utcnow


class NotificationLog(SQLModel, table=True):
    __tablename__ = "notification_log"
    __table_args__ = (
        # Pro User + Reminder-Stufe + Ablaufzyklus genau einmal senden.
        # `expiry_cycle` = ISO-Datum des Ablaufs -> nach Passwortwechsel neuer Zyklus.
        UniqueConstraint("entra_user_id", "reminder_day", "expiry_cycle", name="uq_notif_dedup"),
    )

    id: int | None = Field(default=None, primary_key=True)
    entra_user_id: int = Field(foreign_key="entra_user.id", index=True, nullable=False)
    run_id: int | None = Field(default=None, foreign_key="run.id", index=True)

    reminder_day: int = Field(nullable=False)
    expiry_cycle: str = Field(sa_column=Column(String(32), nullable=False))

    channel: str = Field(sa_column=Column(String(16), nullable=False))  # primary | alternate
    backend: str = Field(sa_column=Column(String(16), nullable=False))  # graph | smtp
    recipient: str = Field(sa_column=Column(String(320), nullable=False))
    language: str = Field(default="de", sa_column=Column(String(8), nullable=False))

    status: str = Field(sa_column=Column(String(16), nullable=False))  # sent | failed
    error: str | None = Field(default=None, sa_column=Column(String(2000)))

    created_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
