"""Instanzweiter Multi-Tenant-Mode-Schalter (Access-Modell/Superadmin-Design, Task 5).

`instance.multi_tenant_mode` ist in `settings_schema.SETTINGS` registriert (Task 1) wie
jeder andere Key -- gespeichert wird er aber IMMER als `setting`-Zeile des DEFAULT-Tenants,
niemals lose instanzweit. Ein roher Owner-Read über `SettingsService.get_all()` würde die
`setting`-Tabelle ALLER Tenants blenden (die Owner-Rolle umgeht RLS, und der Key könnte
theoretisch auch pro Kunde existieren) -- das würde "der Default-Tenant besitzt den
globalen Schalter" unterlaufen. Deshalb wird für jeden Zugriff explizit eine
`tenant_scoped_session(default_tenant_id)` geöffnet (App-Rolle + RLS-GUC), genau wie ein
gewöhnlicher Tenant-Request es täte.

Reused von `auth._user_out` (Anzeige in `UserOut.multi_tenant_mode`) und der
`/admin/instance`-Route (Lesen für alle, Schreiben nur Superadmin).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.tenant_context import tenant_scoped_session
from ..repositories import tenant_repo
from .settings_service import SettingsService

MULTI_TENANT_MODE_KEY = "instance.multi_tenant_mode"


async def read_mode(owner_session: AsyncSession) -> bool:
    """Aktueller Stand von `instance.multi_tenant_mode`, default-tenant-gescopt gelesen."""
    default = await tenant_repo.default_tenant(owner_session)
    assert default.id is not None  # persistierte Zeile, von der Migration angelegt
    async with tenant_scoped_session(default.id) as scoped:
        value = await SettingsService(scoped).get(MULTI_TENANT_MODE_KEY)
    return bool(value)


async def read_default_tenant_name(owner_session: AsyncSession) -> str:
    """`tenant.name` des Default-Tenants (`is_default=true`, NICHT über `slug == 'default'`
    identifiziert -- der Slug ist umbenennbar, siehe `tenant_repo.default_tenant`)."""
    default = await tenant_repo.default_tenant(owner_session)
    return default.name


async def write_mode(owner_session: AsyncSession, value: bool) -> None:
    """Schreibt `instance.multi_tenant_mode` default-tenant-gescopt.

    Nur von der superadmin-gegateten `/admin/instance`-Route aufzurufen -- kein eigenes
    Gate hier, das ist Sache des Aufrufers (siehe `admin_instance.update_instance`)."""
    default = await tenant_repo.default_tenant(owner_session)
    assert default.id is not None
    async with tenant_scoped_session(default.id) as scoped:
        await SettingsService(scoped).set(MULTI_TENANT_MODE_KEY, value)
