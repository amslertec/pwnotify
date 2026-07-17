"""Assignment-Group-Schemas (Console+Groups+Invite-Phase, Task 3).

`entra_group_id` ist in diesem Inkrement FREI-TEXT (Design §7) -- kein Graph-Picker, keine
Format-Validierung ausser der Längenbegrenzung; das ist eine bewusste, explizit als
Phase-2-Erweiterung vertagte Vereinfachung, kein Versehen."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entra_group_id: str = Field(min_length=1, max_length=64)


class GroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class GroupTenants(BaseModel):
    tenant_ids: list[int] = []


class GroupOut(BaseModel):
    id: int
    name: str
    entra_group_id: str
    tenant_ids: list[int]
