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
from .session import get_session_factory

# Aktiver Kunde für den laufenden Task/Request. None = instanzweit (Owner, kein Rollenwechsel).
current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)


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
    """Session, deren Transaktionen automatisch tenant-scoped sind (App-Rolle + GUC)."""
    with _bind_tenant(tenant_id):
        async with get_session_factory()() as session:
            yield session
