"""Settings-bezogene Request-Schemas (Tests, Vorschauen)."""

from __future__ import annotations

from typing import Any, Literal

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


class ExclusionCreate(BaseModel):
    """Body for `POST /settings/exclusions` (L10). Previously an untyped `dict[str, str]`,
    which turned a missing `value` into a `KeyError`/500 and accepted any `kind` string.

    `kind` is pinned to the two values the `Exclusion` model recognises ("user" | "group",
    see `models/entra.py`), defaulting to "user" (the old `body.get("kind", "user")`
    semantics). `value`/`label` are length-capped to the DB columns (`String(320)`)."""

    kind: Literal["user", "group"] = "user"
    value: str = Field(min_length=1, max_length=320)
    label: str | None = Field(default=None, max_length=320)
