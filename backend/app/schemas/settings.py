"""Settings-bezogene Request-Schemas (Tests, Vorschauen)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SettingsUpdate(BaseModel):
    """Partielles Update. Erlaubt beliebige registrierte Keys (Service filtert)."""

    values: dict[str, Any]


class GraphTestRequest(BaseModel):
    # Optional: ungespeicherte Werte aus dem Formular testen (Secret via Masken-Marker).
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    cloud: str | None = None


class GraphTestResult(BaseModel):
    connected: bool
    tenant_id: str | None = None
    granted_permissions: list[str] = Field(default_factory=list)
    missing_permissions: list[str] = Field(default_factory=list)
    error: str | None = None


class MailTestRequest(BaseModel):
    to: str = Field(min_length=3, max_length=320)
    locale: str = "de"


class CronPreviewRequest(BaseModel):
    cron: str
    timezone: str = "Europe/Zurich"


class CronPreviewResult(BaseModel):
    valid: bool
    next_runs: list[str] = Field(default_factory=list)
    error: str | None = None


class TemplatePreviewRequest(BaseModel):
    subject: str
    html: str
    locale: str = "de"


class TemplatePreviewResult(BaseModel):
    subject: str
    html: str
