"""DB-Zugriff für Mandanten (Tenant) + Sitzungs-Autorisierung: welcher AppUser darf welchen
Kunden sehen/verwalten/aktivieren.

Läuft ausschließlich auf der OWNER-Session (kein RLS-Rollenwechsel) -- `tenant`, `app_user`,
`admin_tenant` und `auditor_tenant` sind instanzweite Tabellen, keine RLS-tenant-gescopten
Kundendaten (siehe Migration f7a8b9c0d1e2, die `pwnotify_app` hierauf nur eingeschränkte
Rechte gibt).

Sicherheitsgrenze -- VIER Kontoarten (Access-Modell/Superadmin-Design §2), default-deny bei
jedem unerwarteten Zustand:
- **Superadmin** (`not is_sso and role=="superadmin"`): ALLE AKTIVEN Tenants, lesend wie
  schreibend. Instanzweit, keine Zuweisungszeile nötig.
- **Lokaler Admin** (`not is_sso and role=="admin"`): NUR seine über `admin_tenant`
  zugewiesenen UND aktiven Tenants (nicht mehr "alle" -- das war das alte Drei-Wege-Modell).
- **Lokaler Auditor** (`not is_sso and role=="auditor"`): nur seine über `auditor_tenant`
  zugewiesenen UND aktiven Tenants.
- **SSO-Konto** (`is_sso=True`): sein Heim-`AppUser.tenant_id` (Rolle aus den Gruppen des
  Heim-Tenants, Phase 4c Task 4) UNION alle `admin_tenant`/`auditor_tenant`-Grants -- ein
  SSO-Konto des Haupttenants kann vom Superadmin zusätzlich auf weitere Kunden berechtigt
  werden. Die Kapazität (lesen/schreiben) folgt dabei dem Zuweisungstyp, nicht der Heim-Rolle.
- Alles andere (unbekannte Rolle, fehlende Zuordnung, `tenant_id is None`): leere Menge /
  False / None. Es gibt KEINEN Fallback auf "alle".

Effektive Mengen (Design §2, Kern-Invariante):
- `admin_tenants(user)` = `admin_tenant`-Grants VEREINIGT MIT dem Heim-Tenant (falls
  SSO-Konto mit `role=="admin"`)
- `auditor_tenants(user)` = `auditor_tenant`-Grants VEREINIGT MIT dem Heim-Tenant (falls
  SSO-Konto mit `role=="auditor"`)
- `allowed_tenant_ids(user)` = Vereinigung beider (Superadmin -> `None` = alle aktiven)
- Schreibzugriff auf einen Tenant erfordert Mitgliedschaft in `admin_tenants(user)` (oder
  Superadmin); Lesen erfordert Mitgliedschaft in `allowed_tenant_ids(user)`.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConflictError, NotFoundError
from ..models.tenant import AdminTenant, AuditorTenant, Tenant
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
    """Aktive Tenants, denen ein Konto per `auditor_tenant` zugewiesen ist (lokaler Auditor
    ODER SSO-Grant) -- stabil nach Tenant-Name sortiert, macht `resolve_initial_tenant`
    deterministisch."""
    res = await session.execute(
        select(AuditorTenant.tenant_id)
        .join(Tenant, Tenant.id == AuditorTenant.tenant_id)
        .where(AuditorTenant.user_id == user_id, Tenant.is_active.is_(True))
        .order_by(Tenant.name)
    )
    return list(res.scalars().all())


async def _admin_grant_tenant_ids(session: AsyncSession, user_id: int) -> list[int]:
    """Aktive Tenants, denen ein Konto per `admin_tenant` zugewiesen ist (lokaler Admin
    ODER SSO-Grant) -- Pendant zu `_assigned_active_tenant_ids` für Admin-Zuweisungen,
    gleiche Form (aktiv-gejoint, nach Tenant-Name sortiert)."""
    res = await session.execute(
        select(AdminTenant.tenant_id)
        .join(Tenant, Tenant.id == AdminTenant.tenant_id)
        .where(AdminTenant.user_id == user_id, Tenant.is_active.is_(True))
        .order_by(Tenant.name)
    )
    return list(res.scalars().all())


async def admin_tenants(session: AsyncSession, user: AppUser) -> set[int]:
    """Menge der Tenants, in denen `user` SCHREIBEND agieren darf (Admin-Kapazität):
    `admin_tenant`-Grants vereinigt mit dem Heim-Tenant (falls SSO-Konto mit `role=="admin"`).

    Für den Superadmin NICHT die richtige Funktion -- der ist instanzweit und braucht keine
    Zeile hier; `is_allowed`/`allowed_tenant_ids` behandeln ihn gesondert."""
    granted: set[int] = set()
    if user.id is not None:
        granted = set(await _admin_grant_tenant_ids(session, user.id))
    if user.is_sso and user.role == "admin" and user.tenant_id is not None:
        granted.add(user.tenant_id)
    return granted


async def auditor_tenants(session: AsyncSession, user: AppUser) -> set[int]:
    """Menge der Tenants, in denen `user` LESEND agieren darf (Auditor-Kapazität):
    `auditor_tenant`-Grants vereinigt mit dem Heim-Tenant (falls SSO-Konto mit
    `role=="auditor"`)."""
    granted: set[int] = set()
    if user.id is not None:
        granted = set(await _assigned_active_tenant_ids(session, user.id))
    if user.is_sso and user.role == "auditor" and user.tenant_id is not None:
        granted.add(user.tenant_id)
    return granted


async def allowed_tenant_ids(session: AsyncSession, user: AppUser) -> set[int] | None:
    """None = ALLE aktiven Tenants (nur Superadmin). Sonst eine konkrete, ggf. leere Menge:
    `admin_tenants(user)` vereinigt mit `auditor_tenants(user)` (Design §2).

    Default-Deny: jeder unerwartete Rollen-/Zustandsfall liefert eine leere Menge -- niemals
    ein stiller Fallback auf "alle". **Verhaltensänderung ggü. dem alten Drei-Wege-Modell:**
    ein lokaler Admin ist NICHT mehr `None` -- er ist jetzt seine `admin_tenant`-Zuweisung.
    """
    if not user.is_sso and user.role == "superadmin":
        return None
    return (await admin_tenants(session, user)) | (await auditor_tenants(session, user))


async def is_allowed(
    session: AsyncSession, user: AppUser, tid: int, *, write: bool = False
) -> bool:
    """Autoritatives Gate -- hierüber prüfen, nicht `allowed_tenant_ids` allein auswerten:
    für SSO-Konten fliesst der Heim-Tenant ungefiltert in `admin_tenants`/`auditor_tenants`
    ein; ob dieser eine Tenant (noch) aktiv ist, wird nur hier zusätzlich geprüft.

    `write=True`: Schreibzugriff -- erfordert Mitgliedschaft in `admin_tenants(user)` (oder
    Superadmin). `write=False` (Default): Lesezugriff -- erfordert Mitgliedschaft in
    `allowed_tenant_ids(user)` (oder Superadmin = alle aktiven).
    """
    if not user.is_sso and user.role == "superadmin":
        return await _is_active(session, tid)
    if write:
        return tid in await admin_tenants(session, user) and await _is_active(session, tid)
    allowed = await allowed_tenant_ids(session, user)
    return allowed is not None and tid in allowed and await _is_active(session, tid)


async def resolve_initial_tenant(session: AsyncSession, user: AppUser) -> int | None:
    """Tenant, der beim Login aktiviert wird -- None, wenn es keinen gibt (z.B. Admin/Auditor
    ohne Zuweisung, SSO-Konto ohne `tenant_id`).

    Lokaler Admin: erster (nach Tenant-Name sortierter) `admin_tenant`-Grant -- KEIN
    stiller Fallback auf den Default-Tenant mehr, wenn keine Zuweisung besteht (das alte
    Drei-Wege-Modell gab dem lokalen Admin implizit "alle"/den Default-Tenant; jetzt gilt
    dieselbe Default-Deny-Regel wie beim Auditor: unzugewiesen -> `None` -> 403 beim
    Aufrufer, siehe `_resolve_authorized_tenant`)."""
    if user.is_sso:
        return user.tenant_id
    if user.role == "superadmin":
        return (await default_tenant(session)).id
    if user.role == "admin" and user.id is not None:
        ids = await _admin_grant_tenant_ids(session, user.id)
        return ids[0] if ids else None
    if user.role == "auditor" and user.id is not None:
        ids = await _assigned_active_tenant_ids(session, user.id)
        return ids[0] if ids else None
    return None
