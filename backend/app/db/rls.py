"""Zentrale Konstanten für die Row-Level-Security-Isolation (Phase 2)."""

from __future__ import annotations

# Eingeschränkte DB-Rolle, in die Runtime-Sessions per SET LOCAL ROLE wechseln.
# NON-superuser, NON-BYPASSRLS — nur so greift RLS (der Owner/Superuser umgeht sie).
APP_ROLE = "pwnotify_app"

# Session-GUC, aus dem die RLS-Policies den aktiven Tenant lesen.
TENANT_GUC = "app.current_tenant"

# Tabellen mit Mandanten-Isolation (alle tragen tenant_id).
RLS_TABLES = (
    "entra_user",
    "exclusion",
    "notification_log",
    "run",
    "setting",
    "audit_log",
)
