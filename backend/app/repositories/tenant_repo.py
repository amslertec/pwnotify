"""DB-Zugriff für Mandanten (Tenant) + Sitzungs-Autorisierung: welcher AppUser darf welchen
Kunden sehen/aktivieren.

Läuft ausschließlich auf der OWNER-Session (kein RLS-Rollenwechsel) -- `tenant`, `app_user`
und `auditor_tenant` sind instanzweite Tabellen, keine RLS-tenant-gescopten Kundendaten
(siehe Migration f7a8b9c0d1e2, die `pwnotify_app` hierauf nur eingeschränkte Rechte gibt).

Sicherheitsgrenze -- drei Kontoarten, default-deny bei jedem unerwarteten Zustand:
- Lokaler Admin (`role=="admin"`, `not is_sso`): alle AKTIVEN Tenants.
- Lokaler Auditor (`role=="auditor"`, `not is_sso`): nur seine über `auditor_tenant`
  zugewiesenen UND aktiven Tenants.
- SSO-Konto (`is_sso=True`, jede Rolle): genau `AppUser.tenant_id` -- `is_allowed` prüft
  zusätzlich, dass dieser eine Tenant aktiv ist.
- Alles andere (unbekannte Rolle, fehlende Zuordnung, `tenant_id is None`): leere Menge /
  False / None. Es gibt KEINEN Fallback auf "alle".
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConflictError, NotFoundError
from ..models.tenant import AuditorTenant, Tenant
from ..models.user import AppUser


async def list_active(session: AsyncSession) -> list[Tenant]:
    res = await session.execute(
        select(Tenant).where(Tenant.is_active.is_(True)).order_by(Tenant.name)
    )
    return list(res.scalars().all())


async def get(session: AsyncSession, tid: int) -> Tenant | None:
    return await session.get(Tenant, tid)


async def default_tenant(session: AsyncSession) -> Tenant:
    """Der Default-Tenant (`slug='default'`) -- wird von der Migration angelegt und muss
    daher in jeder migrierten Instanz existieren."""
    res = await session.execute(select(Tenant).where(Tenant.slug == "default"))
    return res.scalar_one()


async def get_by_entra_tid(session: AsyncSession, tid: str) -> Tenant | None:
    """Aktiver Tenant mit passendem Entra-`tid`-Claim (SSO-Auto-Mapping, Task 4)."""
    res = await session.execute(
        select(Tenant).where(Tenant.entra_tenant_id == tid, Tenant.is_active.is_(True))
    )
    return res.scalar_one_or_none()


async def get_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    res = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return res.scalar_one_or_none()


async def _get_by_entra_tid_any(session: AsyncSession, entra_tenant_id: str) -> Tenant | None:
    """Wie `get_by_entra_tid`, aber ohne den `is_active`-Filter -- die CRUD-Eindeutigkeitsprüfung
    muss auch gegen einen bereits deaktivierten Tenant blocken (die Spalte ist global unique,
    unabhängig vom Aktivstatus)."""
    res = await session.execute(select(Tenant).where(Tenant.entra_tenant_id == entra_tenant_id))
    return res.scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Tenant]:
    """ALLE Tenants inkl. inaktiver, für die Verwaltungsübersicht -- anders als `list_active`."""
    res = await session.execute(select(Tenant).order_by(Tenant.name))
    return list(res.scalars().all())


async def count_sso_users(session: AsyncSession, tid: int) -> int:
    res = await session.execute(
        select(func.count(AppUser.id)).where(AppUser.is_sso.is_(True), AppUser.tenant_id == tid)
    )
    return int(res.scalar_one())


async def create(
    session: AsyncSession, *, name: str, slug: str, entra_tenant_id: str | None = None
) -> Tenant:
    if await get_by_slug(session, slug) is not None:
        raise ConflictError("Dieser Slug wird bereits verwendet.", code="tenant_slug_taken")
    if entra_tenant_id is not None and await _get_by_entra_tid_any(session, entra_tenant_id):
        raise ConflictError(
            "Diese Entra-Tenant-ID wird bereits verwendet.", code="tenant_entra_tid_taken"
        )
    tenant = Tenant(name=name, slug=slug, entra_tenant_id=entra_tenant_id)
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def update(
    session: AsyncSession,
    tid: int,
    *,
    name: str | None = None,
    entra_tenant_id: str | None = None,
    is_active: bool | None = None,
) -> Tenant:
    """Nur übergebene Felder anwenden. Der Default-Tenant-Schutz (slug='default' darf nicht
    deaktiviert werden) ist Sache der Route (Task 2), nicht dieser Funktion."""
    tenant = await session.get(Tenant, tid)
    if tenant is None:
        raise NotFoundError("Mandant nicht gefunden.", code="tenant_not_found")
    if entra_tenant_id is not None and entra_tenant_id != tenant.entra_tenant_id:
        existing = await _get_by_entra_tid_any(session, entra_tenant_id)
        if existing is not None and existing.id != tid:
            raise ConflictError(
                "Diese Entra-Tenant-ID wird bereits verwendet.", code="tenant_entra_tid_taken"
            )
    if name is not None:
        tenant.name = name
    if entra_tenant_id is not None:
        tenant.entra_tenant_id = entra_tenant_id
    if is_active is not None:
        tenant.is_active = is_active
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def delete(session: AsyncSession, tid: int) -> None:
    """Nur die reine Zeile -- Kaskade/SSO-Aufräumlogik ist Sache der Route (Task 2)."""
    tenant = await session.get(Tenant, tid)
    if tenant is not None:
        await session.delete(tenant)
        await session.commit()


async def _is_active(session: AsyncSession, tid: int) -> bool:
    t = await session.get(Tenant, tid)
    return t is not None and t.is_active


async def _assigned_active_tenant_ids(session: AsyncSession, user_id: int) -> list[int]:
    """Aktive Tenants, denen ein lokaler Auditor per `auditor_tenant` zugewiesen ist
    (stabil nach Tenant-Name sortiert -- macht `resolve_initial_tenant` deterministisch)."""
    res = await session.execute(
        select(AuditorTenant.tenant_id)
        .join(Tenant, Tenant.id == AuditorTenant.tenant_id)
        .where(AuditorTenant.user_id == user_id, Tenant.is_active.is_(True))
        .order_by(Tenant.name)
    )
    return list(res.scalars().all())


async def allowed_tenant_ids(session: AsyncSession, user: AppUser) -> set[int] | None:
    """None = ALLE aktiven Tenants (nur lokaler Admin). Sonst eine konkrete, ggf. leere Menge.

    Default-Deny: jeder unerwartete Rollen-/Zustandsfall liefert eine leere Menge -- niemals
    ein stiller Fallback auf "alle".
    """
    if user.is_sso:
        return {user.tenant_id} if user.tenant_id is not None else set()
    if user.role == "admin":
        return None
    if user.role == "auditor" and user.id is not None:
        return set(await _assigned_active_tenant_ids(session, user.id))
    return set()


async def is_allowed(session: AsyncSession, user: AppUser, tid: int) -> bool:
    """Autoritatives Gate -- hierüber prüfen, nicht `allowed_tenant_ids` allein auswerten:
    für SSO liefert jene Funktion bewusst ungefiltert die Bindung an `tenant_id`; ob dieser
    eine Tenant (noch) aktiv ist, wird nur hier geprüft."""
    if user.is_sso:
        return (
            user.tenant_id is not None and user.tenant_id == tid and await _is_active(session, tid)
        )
    if user.role == "admin":
        return await _is_active(session, tid)
    if user.role == "auditor" and user.id is not None:
        return tid in await _assigned_active_tenant_ids(session, user.id)
    return False


async def resolve_initial_tenant(session: AsyncSession, user: AppUser) -> int | None:
    """Tenant, der beim Login aktiviert wird -- None, wenn es keinen gibt (z.B. Auditor
    ohne Zuweisung, SSO-Konto ohne `tenant_id`)."""
    if user.is_sso:
        return user.tenant_id
    if user.role == "admin":
        return (await default_tenant(session)).id
    if user.role == "auditor" and user.id is not None:
        ids = await _assigned_active_tenant_ids(session, user.id)
        return ids[0] if ids else None
    return None
