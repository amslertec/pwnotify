"""Tenant-Verwaltungs-Schemas (Phase 4c)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

# Lowercase, Ziffern, einzelne Bindestriche als Trenner -- keine führenden/folgenden/doppelten
# Bindestriche. Wird sowohl als URL-/Log-Bezeichner als auch beim SSO-Auto-Mapping verwendet.
_SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class TenantOut(BaseModel):
    id: int
    name: str
    slug: str
    entra_tenant_id: str | None
    is_active: bool
    created_at: dt.datetime
    # Anzahl per SSO an diesen Tenant gebundener Konten -- Warnsignal für die Route (Task 2),
    # bevor sie einen Tenant löscht/deaktiviert (siehe count_sso_users im Repo).
    sso_user_count: int


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=64, pattern=_SLUG_PATTERN)
    entra_tenant_id: str | None = Field(default=None, max_length=64)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    entra_tenant_id: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
