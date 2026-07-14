"""Key/Value-Einstellungen (laufende App-Konfiguration).

Werte werden als JSON abgelegt. Geheime Werte (``is_secret=True``) sind at-rest
Fernet-verschlüsselt (der SettingsService kümmert sich um En-/Decrypt + Masking).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ._base import utcnow


class Setting(SQLModel, table=True):
    __tablename__ = "setting"

    key: str = Field(sa_column=Column(String(100), primary_key=True))
    value: Any | None = Field(default=None, sa_column=Column(JSONB))
    is_secret: bool = Field(default=False)
    updated_at: dt.datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
