"""Gemeinsame Helfer für Modelle."""

from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# Phase-1 hatte hier TENANT_ID_BRIDGE (server_default=FetchedValue()): ein statischer
# DB-seitiger Default auf die Default-Tenant-id, damit bestehende Writer ohne explizites
# tenant_id weiterliefen. Phase 3 (Task 1) ersetzt das durch einen kontext-abhängigen
# Default -- siehe `default_factory=current_tenant_or_none` direkt an den `tenant_id`-Feldern
# in entra.py/notification.py/run.py/setting.py/audit.py (Import aus
# `app.db.tenant_context`). Es gibt hier bewusst keinen DB-server_default mehr: ein
# ORM-INSERT ohne aktiven Tenant-Kontext soll mit NOT NULL fehlschlagen, nicht still auf
# den Default-Tenant zurückfallen.
