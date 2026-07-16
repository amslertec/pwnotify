"""Gemeinsame Helfer für Modelle."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import FetchedValue


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# server_default=FetchedValue() ist Phase-1-Brücke: der eigentliche Default (Default-Tenant-
# id) sitzt als server_default in der Migration; dieser Marker sagt SQLAlchemy nur, dass
# einer existiert, damit tenant_id bei fehlendem Wert aus dem INSERT weggelassen (statt
# explizit NULL gesendet) wird und Postgres den Default anwendet. Ohne diesen Marker würde
# die ORM-Schicht (session.add(...)) trotz DB-seitigem server_default NULL einfügen.
TENANT_ID_BRIDGE = {"server_default": FetchedValue()}
