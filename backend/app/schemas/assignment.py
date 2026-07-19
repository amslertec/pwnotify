"""Zuweisungs-Schemas (Access-Modell/Superadmin-Phase, Task 4; Bulk-Erweiterung Task 2 der
Console+Groups+Invite-Phase).

Bewusste Abweichung von der ursprünglichen Planung (Task-4-Brief): der Zuweisungstyp
(`admin_tenant` vs. `auditor_tenant`) wird NICHT vom Aufrufer per Dual-Liste
(`{admin:[...], auditor:[...]}`) gewählt, sondern strukturell aus der ROLLE des
Zielkontos abgeleitet (siehe `api/routes/admin_assignments.py`). `AssignmentUpdate` kennt
deshalb nur EINE `tenant_ids`-Liste -- keinen `kind`-/`admin`-/`auditor`-Schlüssel, über
den ein Aufrufer Grant-Typ und Rolle auseinanderlaufen lassen könnte.

`BulkAssignmentUpdate`/`BulkAssignmentResult` (Task 2, `PUT /admin/assignments/bulk`):
Report-statt-Fehlschlag-Semantik für einzelne Konten (`skipped`, s. `SkippedUser.reason`),
weil ein cross-grant-gesperrtes oder unbekanntes Konto in einer Batch-Anfrage keine
Anfrage-weite Ablehnung rechtfertigt -- die übrigen Konten der Charge sollen trotzdem
durchlaufen (siehe Moduldoku in `api/routes/admin_assignments.py` für die Abgrenzung
gegen den harten Fehlschlag bei einer ungültigen `tenant_id`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AssignmentOut(BaseModel):
    role: str
    tenant_ids: list[int]


# Caps below guard against unbounded request payloads (L6): 500 for tenant IDs (there are
# far fewer customers than users, so this leaves generous headroom over any realistic
# tenant count), 2000 for user IDs (covers large-org bulk assignment batches without
# accepting an arbitrarily large list).
class AssignmentUpdate(BaseModel):
    tenant_ids: list[int] = Field(default_factory=list, max_length=500)


class BulkAssignmentUpdate(BaseModel):
    user_ids: list[int] = Field(max_length=2000)
    tenant_ids: list[int] = Field(max_length=500)
    action: Literal["add", "remove", "set"]


class SkippedUser(BaseModel):
    user_id: int
    reason: str


class BulkAssignmentResult(BaseModel):
    updated: list[int]
    skipped: list[SkippedUser]
