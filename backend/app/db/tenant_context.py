"""Aktiver-Tenant-Kontext + tenant-scoped Session (Phase-2-Isolationskern).

Der aktive Tenant lebt in einem ContextVar. Ein begin-Event-Listener (in session.py registriert)
trägt ihn bei jedem Transaktionsbeginn als SET LOCAL ROLE + SET LOCAL app.current_tenant ein.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Iterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .rls import APP_ROLE, TENANT_GUC
from .session import get_runtime_session_factory

# Aktiver Kunde für den laufenden Task/Request. None = instanzweit (Owner, kein Rollenwechsel).
current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)


def current_tenant_or_none() -> int | None:
    """Aktiver Tenant für kontext-abhängige Column-Defaults (INSERT-Stempel).

    Wird als ``default_factory``/Spalten-Default auf ``tenant_id`` der Tenant-Tabellen
    verwendet: läuft ein Schreibzugriff innerhalb von ``tenant_scoped_session``/``use_tenant``,
    stempelt er automatisch den aktiven Tenant; ohne Kontext (Owner) liefert er ``None`` --
    das NOT-NULL-Constraint der fünf Datentabellen macht daraus dann bewusst einen Fehler
    (kein stiller Fallback mehr, siehe Phase-1-Brücke in `_base.py`, jetzt entfernt).
    """
    return current_tenant_id.get()


@contextlib.contextmanager
def _bind_tenant(tenant_id: int | None) -> Iterator[None]:
    token = current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        current_tenant_id.reset(token)


@contextlib.asynccontextmanager
async def use_tenant(tenant_id: int) -> AsyncGenerator[None]:
    """Setzt den aktiven Tenant für den umschlossenen async-Block."""
    with _bind_tenant(tenant_id):
        yield


@contextlib.contextmanager
def use_owner_context() -> Iterator[None]:
    """Vorübergehend in den Owner-Kontext (kein aktiver Tenant) wechseln.

    Anders als ``use_tenant`` NICHT verschachtelungssicher gegenüber sich selbst, aber
    genau dafür gedacht: innerhalb eines bereits aktiven ``use_tenant``-Blocks (z. B. im
    Hintergrund-Lauf pro Kunde) gibt es Schreibzugriffe, die instanzweit bleiben müssen
    (z. B. `app_user` beim SSO-Abgleich). Eine Session, die WÄHREND dieses Blocks geöffnet
    wird, läuft wieder als Owner (kein Rollenwechsel, kein GUC) -- danach greift der
    umschliessende Tenant-Kontext unverändert weiter.
    """
    with _bind_tenant(None):
        yield


def apply_tenant_on_begin(dbapi_connection: Any, connection_record: object) -> None:
    """SQLAlchemy 'begin'-Listener: bei aktivem Tenant in die App-Rolle wechseln + GUC setzen.

    Läuft synchron auf der rohen DBAPI-Verbindung, aber im selben Greenlet wie der umgebende
    async-Aufruf (SQLAlchemys asyncio-Bridge) -> der ContextVar ist sichtbar. SET LOCAL gilt
    nur innerhalb der laufenden Transaktion; deshalb bei JEDEM begin neu.

    Bind-Parameter sind hier bewusst NICHT verwendet: Postgres' SET/SET LOCAL akzeptiert keine
    Parameter-Platzhalter für den Wert (nur Literale -- empirisch geprüft: `SET LOCAL x = $1`
    scheitert mit `PostgresSyntaxError`). tenant_id ist ein `int`, der ausschließlich über
    `use_tenant`/`tenant_scoped_session` (beide `tenant_id: int`) in den ContextVar gelangt --
    die `int(...)`-Erzwingung unten macht eine String-Interpolation hier ungefährlich
    (kein Nutzereingabe-Pfad, kein Injection-Vektor).
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        return  # instanzweit: als Owner belassen (Migrationen, Login vor Tenant-Wahl)
    cur = dbapi_connection.cursor()
    try:
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        cur.execute(f"SET LOCAL {TENANT_GUC} = '{int(tenant_id)}'")
    finally:
        cur.close()


@contextlib.asynccontextmanager
async def tenant_scoped_session(tenant_id: int) -> AsyncGenerator[AsyncSession]:
    """Session, deren Transaktionen automatisch tenant-scoped sind (App-Rolle + GUC).

    Läuft über die Runtime-Engine (Login-Rolle `pwnotify_runtime`, NOSUPERUSER/NOBYPASSRLS,
    Mitglied von `pwnotify_app`) statt über die Owner-Engine: selbst ein `RESET ROLE` aus
    dieser Session heraus landet auf `pwnotify_runtime`, nicht auf dem Owner/Superuser -- RLS
    bleibt in jedem Fall wirksam (siehe `app/db/session.py::get_runtime_session_factory`).
    """
    with _bind_tenant(tenant_id):
        async with get_runtime_session_factory()() as session:
            yield session
