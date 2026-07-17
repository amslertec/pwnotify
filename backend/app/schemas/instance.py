"""Schemas für den instanzweiten Multi-Tenant-Mode-Schalter (Access-Modell Task 5)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InstanceOut(BaseModel):
    multi_tenant_mode: bool
    default_tenant_name: str


class InstanceUpdate(BaseModel):
    """Partielles Update -- beide Felder optional, nur übergebene werden geschrieben."""

    multi_tenant_mode: bool | None = None
    default_tenant_name: str | None = Field(default=None, min_length=1, max_length=320)
