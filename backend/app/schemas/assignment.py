"""Zuweisungs-Schemas (Access-Modell/Superadmin-Phase, Task 4).

Bewusste Abweichung von der ursprünglichen Planung (Task-4-Brief): der Zuweisungstyp
(`admin_tenant` vs. `auditor_tenant`) wird NICHT vom Aufrufer per Dual-Liste
(`{admin:[...], auditor:[...]}`) gewählt, sondern strukturell aus der ROLLE des
Zielkontos abgeleitet (siehe `api/routes/admin_assignments.py`). `AssignmentUpdate` kennt
deshalb nur EINE `tenant_ids`-Liste -- keinen `kind`-/`admin`-/`auditor`-Schlüssel, über
den ein Aufrufer Grant-Typ und Rolle auseinanderlaufen lassen könnte.
"""

from __future__ import annotations

from pydantic import BaseModel


class AssignmentOut(BaseModel):
    role: str
    tenant_ids: list[int]


class AssignmentUpdate(BaseModel):
    tenant_ids: list[int] = []
