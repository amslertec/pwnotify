"""Benutzerverwaltung: lokale Konten (CRUD) + SSO-Konten (aus Entra-Gruppe)."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ...core.config import get_settings
from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...core.security import hash_password
from ...db.tenant_context import tenant_scoped_session
from ...models._base import utcnow
from ...models.user import AppUser
from ...repositories import tenant_repo, user_repo, user_token_repo
from ...schemas.auth import (
    AdminUserCreate,
    AdminUserOut,
    RoleUpdate,
    SuperadminCreate,
    SuperadminToggle,
)
from ...schemas.common import Message
from ...services import audit, user_token
from ...services.settings_service import SettingsService
from ..deps import (
    ActiveTenantClaim,
    AdminUser,
    CurrentUser,
    SessionDep,
    SuperadminDefaultContextUser,
    default_tenant_id,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


# --------------------------------------------------------------------------- #
# Profilbild-Pfad (Task B) -- eigene, minimale Kopie von `auth.py`s `_avatar_path`
# (dort `_avatar_dir()` + `_avatar_path(user_id)`, Zeile 89/95), NICHT von dort
# importiert: `auth.py` ist für diesen Task read-only (Referenz), ein Import würde
# ausserdem unnötig an dessen Upload-/2FA-Import-Baum koppeln. `get_settings()` wird
# bewusst PRO AUFRUF (nicht einmalig modulweit gecacht wie `auth.py`s `_settings`)
# gelesen -- so greifen Tests, die `PWNOTIFY_DATA_DIR` + `get_settings.cache_clear()`
# nutzen (Muster aus `test_branding_tenant_scope.py`), auch nach diesem Modul-Import.
# Kein `mkdir` hier (anders als `auth.py`s `_avatar_dir`): diese Seite liest nur,
# legt nie ab -- ein nicht existierendes Verzeichnis ist einfach "kein Avatar".
def _avatar_path(user_id: int) -> Path:
    return Path(get_settings().data_dir) / "avatars" / f"{user_id}.png"


def _avatar_mtime(user_id: int) -> int | None:
    """mtime des Profilbilds als Cache-Buster, oder `None` wenn kein Bild existiert bzw. das
    `data_dir` nicht lesbar ist. Jeder `OSError` (fehlende Datei, nicht erreichbares `/data`
    -- z. B. frischer Deploy vor Volume-Mount oder CI ohne `/data`) -> "kein Avatar", damit
    die Nutzer-Serialisierung nie an einem Dateisystemzustand 500t (`Path.exists()` würde ein
    `EACCES` durchreichen statt es als "nein" zu werten)."""
    try:
        return int(_avatar_path(user_id).stat().st_mtime)
    except OSError:
        return None


def _admin_user_out(user: AppUser) -> AdminUserOut:
    """`AdminUserOut.model_validate(..., from_attributes=True)` + die dateibasierten
    Avatar-Felder (Task B) -- die füllt `from_attributes` NICHT, `app_user` hat keine
    entsprechenden Spalten. `avatar_version` ist die mtime als Cache-Buster, exakt wie
    `auth.py`s `UserOut`-Aufbau es für das eigene Profilbild vormacht."""
    out = AdminUserOut.model_validate(user, from_attributes=True)
    if user.id is not None:
        mtime = _avatar_mtime(user.id)
        if mtime is not None:
            out.has_avatar = True
            out.avatar_version = mtime
    return out


@router.get("")
async def list_users(
    user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> dict[str, list[AdminUserOut]]:
    """Gescopte Kontoliste für die Access-Seite (Access-Rescope, Sicherheitsfix).

    **Der Sicherheitsfix:** vormals sah ein Superadmin über `user_repo.list_all(session)`
    IMMER die volle, instanzweite Kontoliste -- unabhängig vom aktiven Mandanten. Beim
    Wechsel zwischen Kunden zeigte die Access-Seite also jedes Mal dieselbe globale Liste,
    statt sich mitzuwechseln. Jetzt gilt für JEDEN Aufrufer (Superadmin eingeschlossen) das
    bestätigte Modell: die Access-Seite zeigt ausschliesslich Konten, deren HEIMAT
    (`app_user.tenant_id`) der AKTIVE Mandant ist.

    **Aktiven Mandanten auflösen:** der rohe `active_tenant`-Claim (`ActiveTenantClaim`,
    unautorisiert, s. `deps.py`) falls vorhanden, sonst der Default-Tenant
    (`deps.default_tenant_id`) -- dieselbe Fallback-Regel wie beim Login/Tenant-Wechsel.

    **Autorisierung:** der aufgelöste Tenant wird IMMER geprüft, bevor er etwas liefert.
    Ein Superadmin darf jeden (aktiven) Tenant sehen -- keine zusätzliche Prüfung nötig.
    Jeder andere Aufrufer muss `tenant_repo.is_allowed(session, user, tid)` bestehen; sonst
    default-deny (leere Listen) -- das verhindert, dass ein lokaler Admin über einen
    gefälschten/veralteten `active_tenant`-Claim einen Tenant auflistet, den er gar nicht
    hält.

    Ergebnis pro Rolle:
    - **Superadmin** (`not is_sso and role=='superadmin'`): Heim-Konten des aktiven
      Tenants (lokal + SSO). Zusätzlich die eigene `superadmins`-Liste (instanzweit, ALLE
      Superadmins) -- aber NUR, wenn der aktive Tenant der DEFAULT-Tenant ist (Provider-
      Kontext); in einem Kunden-Kontext fehlt der `superadmins`-Schlüssel komplett, auch
      für den Superadmin. Provider-Personal (heim am Default-Tenant) erscheint deshalb nur
      in der Default-Ansicht, nicht in irgendeiner Kunden-Ansicht -- Cross-Tenant-Zuweisungen
      laufen über `/admin/assignments`, nicht über diese Seite.
    - **Lokaler Admin** (`not is_sso and role=='admin'`): Heim-Konten NUR des aktiven
      Tenants (lokal + SSO), sofern er diesen Tenant hält (s. Autorisierung oben). Nie
      Superadmins, nie ein `superadmins`-Schlüssel.
    - **Alles andere** (Auditor, SSO-Konto, unbekannter Zustand): default-deny -> leere
      Listen. Die `/access`-Seite ist zwar admin-only im Frontend, dieses Gate gilt aber
      unabhängig davon hier ebenfalls.
    """
    if user.is_sso or user.role not in ("admin", "superadmin"):
        return {"local": [], "sso": []}

    is_superadmin_caller = user.role == "superadmin"
    tid = active_tenant if active_tenant is not None else await default_tenant_id(session)

    if not is_superadmin_caller and not await tenant_repo.is_allowed(session, user, tid):
        return {"local": [], "sso": []}

    local_rows = await user_repo.list_local_homed_in_tenant(session, tid)
    sso_rows = await user_repo.list_sso_in_tenants(session, {tid})
    out: dict[str, list[AdminUserOut]] = {
        "local": [_admin_user_out(u) for u in local_rows],
        "sso": [_admin_user_out(u) for u in sso_rows],
    }

    if is_superadmin_caller and tid == await default_tenant_id(session):
        superadmin_rows = [u for u in await user_repo.list_all(session) if u.role == "superadmin"]
        out["superadmins"] = [_admin_user_out(u) for u in superadmin_rows]

    return out


@router.post("", response_model=AdminUserOut)
async def create_local(
    request: Request,
    admin: AdminUser,
    body: AdminUserCreate,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> AdminUserOut:
    """Legt ein lokales Konto an -- gescopt nach Aufrufer (Task 3).

    Superadmin: uneingeschränkt, KEINE automatische Zuweisung (Tenants weist der
    Superadmin später gezielt zu, Task 4). Jeder andere Admin-Aufrufer (lokaler Admin
    oder SSO-Admin): das neue Konto wird automatisch auf den AKTIVEN Tenant des
    Aufrufers zugewiesen -- mit der zur neuen Rolle passenden Zuweisungsart
    (`role=='admin'` -> `admin_tenant`, `role=='auditor'` -> `auditor_tenant`), damit ein
    `role=='admin'`-Konto nie NUR eine `auditor_tenant`-Zuweisung hat (das würde ihm über
    das Rollen-Gate Schreibzugriff verschaffen, den die Zuweisung selbst nicht hergibt).

    Der `active_tenant`-Claim wird NICHT blind übernommen (er ist laut `ActiveTenantClaim`
    unautorisiert, nur zur Anzeige gedacht) -- stattdessen zusätzlich über
    `tenant_repo.is_allowed(..., write=True)` geprüft. Fehlt der Claim oder besteht keine
    Schreib-Mitgliedschaft, wird klar abgelehnt statt ein unsichtbares, nicht zugewiesenes
    Konto anzulegen.

    **Heim-Tenant setzen (Context-Gating v2, Task 3):** vormals bekam das neue Konto zwar
    eine `admin_tenant`/`auditor_tenant`-Zeile, aber NIE einen `tenant_id` (Heimat) -- damit
    hatte der Cross-Grant-Lock (Task 2, `tenant_repo.is_provider_account`,
    `admin_assignments.set_assignments`) keine Grundlage: ein Konto ohne Heimat gilt dort
    als Kunden-Konto mit LEERER erlaubter Menge (`tenant_id is None` -> kein Provider), also
    über-restriktiv für ein vom Superadmin angelegtes Konto UND ohne echte Kundenheimat für
    ein vom Kunden-Admin angelegtes Konto. Deshalb jetzt explizit:
    - Nicht-Superadmin-Aufrufer (lokaler/SSO-Admin für seinen aktiven Kunden): Heimat =
      `grant_tenant_id` (derselbe, bereits über `is_allowed(..., write=True)` geprüfte aktive
      Tenant) -- das neue Konto ist also kunden-beheimatet UND passend zugewiesen, damit laut
      Task 2 strukturell nicht auf einen fremden Tenant cross-grantbar.
      -- ein reines Kundenstaff-Konto.
    - Superadmin-Aufrufer: Heimat = der Default-Tenant (`deps.default_tenant_id`) -- Provider-
      Personal ist default-beheimatet, ein so angelegtes Konto bleibt daher über die
      Zuweisungs-API (Task 4/Cross-Grant-Lock Task 2) auf beliebige Kunden cross-grantbar.
      (Superadmin-Anlage eines *Superadmin*-Kontos bleibt unverändert in `create_superadmin`
      -- instanzweit, keine Heimat nötig.)

    **Einladungsmodus (Task 5, §7b):** `body.password` ABWESEND schaltet auf Einladung um --
    `body.username` wird dabei bewusst NICHT vom Aufrufer übernommen, sondern die Route
    vergibt einen garantiert eindeutigen, klar nicht einlogg-baren Platzhalter
    (`pending:<uuid4>`) + einen unbrauchbaren Passwort-Hash (`hash_password(secrets.
    token_hex(32))`, kein bekanntes Klartext-Passwort existiert dafür) + `is_active=False`.
    Das vermeidet Schema-Churn (kein Nullable-`username`); der Accept-Endpunkt
    (`api/routes/public_tokens.py`) überschreibt den Platzhalter beim Einlösen mit dem
    echten, dort erst eindeutigkeitsgeprüften Namen. Heim-Tenant + Zuweisung laufen exakt
    wie oben (unverändert nach Aufrufer-Rolle) -- der Einladungsmodus ändert NUR, WOHER die
    Konto-Identität kommt, nie die Scoping-Regeln.
    """
    raw_password = body.password
    is_invite = raw_password is None

    username: str
    password_hash: str
    if raw_password is None:
        if not body.email:
            raise ForbiddenError(
                "Für eine Einladung ist eine E-Mail-Adresse erforderlich.",
                code="email_required",
            )
        username = f"pending:{uuid.uuid4().hex}"
        password_hash = hash_password(secrets.token_hex(32))  # nie einlösbar
    else:
        if not body.username:
            raise ForbiddenError("Benutzername erforderlich.", code="username_required")
        existing = await user_repo.get_by_username(session, body.username)
        if existing is not None:
            raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
        username = body.username
        password_hash = hash_password(raw_password)

    is_superadmin_caller = not admin.is_sso and admin.role == "superadmin"
    grant_tenant_id: int | None = None
    if not is_superadmin_caller:
        if active_tenant is None or not await tenant_repo.is_allowed(
            session, admin, active_tenant, write=True
        ):
            raise ForbiddenError(
                "Kein aktiver Mandant mit Verwaltungsrechten.", code="tenant_required"
            )
        grant_tenant_id = active_tenant

    home_tenant_id = (
        grant_tenant_id if not is_superadmin_caller else await default_tenant_id(session)
    )

    user = await user_repo.create(
        session,
        username=username,
        password_hash=password_hash,
        display_name=body.display_name,
        role=body.role,
        is_sso=False,
        tenant_id=home_tenant_id,
    )
    assert user.id is not None  # gerade committet, hat also eine id

    if is_invite:
        # Einladung: pending -- Konto existiert, ist aber bis zur Annahme (`public_tokens.
        # accept_token`) nicht nutzbar. E-Mail wird hier gesetzt (Reset-Trigger-Anker §7c),
        # nicht am `create()`-Aufruf oben (der bleibt unverändert für den Direktpfad).
        user.email = body.email
        user.is_active = False
        user.updated_at = utcnow()
        await session.commit()
        await session.refresh(user)

    if grant_tenant_id is not None:
        kind = "admin" if body.role == "admin" else "auditor"
        await tenant_repo.add_grant(session, user_id=user.id, tenant_id=grant_tenant_id, kind=kind)

    detail: dict[str, object] = {"role": body.role, "sso": False, "home_tenant_id": home_tenant_id}
    if grant_tenant_id is not None:
        detail["granted_tenant_id"] = grant_tenant_id
    if is_invite:
        detail["email"] = body.email

    await audit.record(
        session,
        action=audit.USER_INVITED if is_invite else audit.USER_CREATED,
        actor=admin,
        target=username,
        request=request,
        detail=detail,
    )
    await session.commit()

    if is_invite:
        assert admin.id is not None
        await user_token.issue_invite(session, user=user, created_by=admin.id)

    return _admin_user_out(user)


@router.post("/{user_id}/reset", response_model=Message)
async def send_reset(
    request: Request, admin: AdminUser, user_id: int, session: SessionDep
) -> Message:
    """Löst einen Passwort-Reset-Link für ein BESTEHENDES lokales Konto aus (Task 5, §7c).

    **Autorisierung:** dieselbe Teilmengen-Regel wie `set_role`/`delete_user` (s. dort für
    die ausführliche Begründung) -- ein Superadmin-Aufrufer überspringt sie (voller
    Zugriff); jeder andere Aufrufer braucht die GESAMTE Tenant-Zugehörigkeit des Ziels
    innerhalb seiner eigenen verwalteten Tenants (Teilmengen-, nicht Schnittmengen-Regel).
    Ein Ziel ganz ohne Tenant-Zugehörigkeit ist NUR einem Superadmin zugänglich.

    **Business-Guards danach** (Reihenfolge bewusst: erst autorisieren, dann validieren):
    ein SSO-Ziel lehnt ab (`sso_no_reset` -- dessen Passwort lebt in Entra, ein lokaler
    Reset-Link wäre wirkungslos/irreführend); ein Ziel ohne hinterlegte E-Mail lehnt
    ebenfalls ab (`email_required` -- der Admin muss sie zuerst im Bearbeiten-Dialog
    setzen, es gibt keine Adresse, an die der Link gehen könnte).

    Mint + Versand laufen über `services.user_token.issue_reset` (entwertet dabei
    idempotent ältere, noch gültige Reset-Tokens desselben Kontos, s. dort)."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")

    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )

    if target.is_sso:
        raise ForbiddenError(
            "SSO-Konten setzen ihr Passwort über Microsoft Entra zurück.",
            code="sso_no_reset",
        )
    if target.email is None:
        raise ForbiddenError(
            "Für dieses Konto ist keine E-Mail-Adresse hinterlegt.", code="email_required"
        )

    assert admin.id is not None
    await user_token.issue_reset(session, user=target, created_by=admin.id)

    await audit.record(
        session,
        action=audit.PASSWORD_RESET_SENT,
        actor=admin,
        target=target.username,
        request=request,
        detail={"target_user_id": user_id},
    )
    await session.commit()
    return Message(message="Link zum Zurücksetzen des Passworts wurde versendet.")


@router.post("/{user_id}/role", response_model=AdminUserOut)
async def set_role(
    request: Request, admin: AdminUser, user_id: int, body: RoleUpdate, session: SessionDep
) -> AdminUserOut:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # Ein Superadmin-Ziel läuft NIE über diesen Pfad (Task 4, Access-Modell/Superadmin-
    # Phase): dieses Gate ist `AdminUser`, nicht `SuperadminUser` -- ein PLAIN Admin
    # könnte sonst über `RoleUpdate.role='admin'` (vom Schema erlaubt) den letzten
    # Superadmin unbemerkt zu einem gewöhnlichen Admin herabstufen, ohne dass der
    # Last-Superadmin-Schutz (der nur in `set_superadmin` sitzt) je greift. Der Wechsel
    # zum/vom Superadmin läuft ausschliesslich über `set_superadmin` (superadmin-only).
    if target.role == "superadmin":
        raise ForbiddenError(
            "Superadmin-Rollenwechsel nur über die Superadmin-Verwaltung möglich.",
            code="superadmin_required",
        )
    # Cross-Tenant-Fix (Sicherheitsreview, Whole-Branch-Review Access-Modell/Superadmin-
    # Phase): Task 3 hat `list_users`/`create_local` gescopt, aber `set_role` blieb nur über
    # `AdminUser` (jeder Admin/Superadmin JEDER Tenant) gegatet und löste `target` ohne RLS
    # auf `app_user` (instanzweit) auf -- ein lokaler Admin von Tenant A konnte so die Rolle
    # eines Kontos ändern, das AUSSCHLIESSLICH zu Tenant B gehört (IDs sind sequentiell
    # enumerierbar). Ein Superadmin-Aufrufer überspringt diese Prüfung (voller Zugriff,
    # bereits durch den obigen Guard beschränkt). Für jeden anderen Aufrufer muss die GESAMTE
    # Tenant-Zugehörigkeit des Ziels innerhalb der vom Aufrufer VERWALTETEN Tenants liegen
    # (Teilmengen-Regel, nicht bloss Schnittmenge) -- `app_user` ist instanzweit, ein Konto
    # kann also zusätzlich einem Tenant angehören, den der Aufrufer nicht hält; ein reiner
    # Schnittmengen-Test würde die Rollenänderung trotzdem durchlassen und so ungewollt auch
    # den fremden Tenant treffen. Ein Ziel ganz ohne Tenant-Zugehörigkeit (leere Menge) darf
    # NUR ein Superadmin anfassen -- daher `not target_scope` als eigener Ablehnungsgrund.
    if admin.is_sso or admin.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, admin)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Den letzten Administrator nicht herabstufen — sonst kann niemand mehr verwalten.
    # Deckt auch den Selbstentzug ab, wenn man der einzige Admin ist.
    if (
        target.role == "admin"
        and body.role != "admin"
        and await user_repo.count_admins(session) <= 1
    ):
        raise ConflictError(
            "Der letzte Administrator kann nicht herabgestuft werden.",
            code="cannot_demote_last_admin",
        )
    vorher = target.role
    target.role = body.role
    await audit.record(
        session,
        action=audit.USER_ROLE_CHANGED,
        actor=admin,
        target=target.username,
        request=request,
        detail={"from": vorher, "to": body.role, "sso": target.is_sso},
    )
    await session.commit()
    await session.refresh(target)
    return _admin_user_out(target)


@router.post("/superadmin", response_model=AdminUserOut)
async def create_superadmin(
    request: Request,
    admin: SuperadminDefaultContextUser,
    body: SuperadminCreate,
    session: SessionDep,
) -> AdminUserOut:
    """Legt einen LOKALEN Superadmin an -- superadmin-only (Design §11.3: Superadmin ist
    IMMER ein lokales Konto, nie SSO). KEINE automatische Zuweisung: der Superadmin ist
    instanzweit und braucht keine `admin_tenant`/`auditor_tenant`-Zeile (anders als
    `create_local` für gewöhnliche Admin/Auditor-Konten, Task 3).

    Seit Context-Gating v2 (Matrix B) zusätzlich nur im DEFAULT-Kontext
    (`SuperadminDefaultContextUser`, `default_context_required`): die Superadmin-Verwaltung
    ist Provider-Ebene (Design §4/§4-notes), genau wie Instanz-/Mandanten-/Zuweisungs-
    Konsole -- aus einem Kunden-Kontext heraus gesperrt.

    **Einladungsmodus (Task 10, Parität zu `create_local`s Einladungsmodus, Task 5, §7b):**
    `body.password` ABWESEND schaltet auf Einladung um -- exaktes Muster wie dort (Platzhalter-
    Benutzername `pending:<uuid4>`, unbrauchbarer Passwort-Hash, `is_active=False`), ABER
    OHNE `add_grant` (Superadmin ist instanzweit, keine Tenant-Zuweisung nötig) und mit
    `tenant_id = default_tenant_id(...)` als Heimat -- NICHT, weil der Superadmin irgendeinen
    Tenant "gehört", sondern weil der Einladungsversand (`user_token._send`) in
    `tenant_scoped_session(user.tenant_id)` läuft und so das Branding auflöst; ein heimatloses
    Konto (`tenant_id=None`, wie im Direktpfad unten -- dort bewusst unverändert, kein
    Mailversand nötig) hätte keinen Branding-Scope. Der Accept-Endpunkt
    (`public_tokens.accept_token`) ist ROLLENAGNOSTISCH -- er fasst `target.role` nie an --,
    daher aktiviert ein mit `role='superadmin'` angelegtes `pending`-Konto korrekt als
    Superadmin, ganz ohne Änderung an `public_tokens.py`/`user_token*.py`."""
    if body.is_sso:
        raise ConflictError(
            "Ein Superadmin muss ein lokales Konto sein.", code="superadmin_must_be_local"
        )

    raw_password = body.password
    is_invite = raw_password is None

    username: str
    password_hash: str
    if raw_password is None:
        if not body.email:
            raise ForbiddenError(
                "Für eine Einladung ist eine E-Mail-Adresse erforderlich.",
                code="email_required",
            )
        username = f"pending:{uuid.uuid4().hex}"
        password_hash = hash_password(secrets.token_hex(32))  # nie einlösbar
    else:
        if not body.username:
            raise ForbiddenError("Benutzername erforderlich.", code="username_required")
        existing = await user_repo.get_by_username(session, body.username)
        if existing is not None:
            raise ConflictError("Benutzername bereits vergeben.", code="username_taken")
        username = body.username
        password_hash = hash_password(raw_password)

    user = await user_repo.create(
        session,
        username=username,
        password_hash=password_hash,
        display_name=body.display_name,
        role="superadmin",
        is_sso=False,
        tenant_id=await default_tenant_id(session) if is_invite else None,
    )
    assert user.id is not None  # gerade committet, hat also eine id

    if is_invite:
        # Einladung: pending -- Konto existiert, ist aber bis zur Annahme (`public_tokens.
        # accept_token`) nicht nutzbar. E-Mail wird hier gesetzt (wie `create_local`s
        # Einladungspfad), nicht am `create()`-Aufruf oben (der bleibt für den Direktpfad
        # unverändert).
        user.email = body.email
        user.is_active = False
        user.updated_at = utcnow()
        await session.commit()
        await session.refresh(user)

    await audit.record(
        session,
        action=audit.USER_INVITED if is_invite else audit.SUPERADMIN_CREATED,
        actor=admin,
        target=username,
        request=request,
        detail={"role": "superadmin", "sso": False, "email": body.email} if is_invite else None,
    )
    await session.commit()

    if is_invite:
        assert admin.id is not None
        await user_token.issue_invite(session, user=user, created_by=admin.id)

    return _admin_user_out(user)


@router.post("/{user_id}/superadmin", response_model=AdminUserOut)
async def set_superadmin(
    request: Request,
    admin: SuperadminDefaultContextUser,
    user_id: int,
    body: SuperadminToggle,
    session: SessionDep,
) -> AdminUserOut:
    """Befördert/degradiert zum/vom Superadmin -- der EINZIGE Pfad dafür (`set_role` lehnt
    jeden Rollenwechsel eines Superadmin-Ziels hart ab, s.o.). Superadmin-only.

    Seit Context-Gating v2 (Matrix B) zusätzlich nur im DEFAULT-Kontext
    (`SuperadminDefaultContextUser`, `default_context_required`): dieselbe Provider-Ebene-
    Begründung wie bei `create_superadmin` oben.

    Befördern: nur ein LOKALES Ziel (`not is_sso`) darf Superadmin werden (Design §11.3,
    `code="superadmin_must_be_local"`) -- seine bisherigen `admin_tenant`/
    `auditor_tenant`-Zuweisungen werden dabei geräumt (bewusste Entscheidung: der
    Superadmin sieht ohnehin alle aktiven Tenants, verwaiste Zuweisungszeilen wären reiner
    Datenmüll und würden bei einer künftigen Rückstufung sonst überraschend wieder
    aufleben).

    Degradieren: der letzte AKTIVE Superadmin darf nicht herabgestuft werden (Design §11.4,
    `code="cannot_demote_last_superadmin"`) -- sonst könnte sich niemand mehr instanzweit
    verwalten. Das Ziel fällt dabei auf `role="admin"` zurück (keine feinere Rolle unterhalb
    von Superadmin ist hier definiert)."""
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")

    if body.promote:
        if target.role == "superadmin":
            return _admin_user_out(target)
        if target.is_sso:
            raise ConflictError(
                "Nur lokale Konten können zu Superadmin befördert werden.",
                code="superadmin_must_be_local",
            )
        vorher = target.role
        target.role = "superadmin"
        assert target.id is not None  # bereits persistiert (kam aus user_repo.get)
        for existing_kind in ("admin", "auditor"):
            for tid in await tenant_repo.list_grant_tenant_ids(session, target.id, existing_kind):
                await tenant_repo.remove_grant(
                    session, user_id=target.id, tenant_id=tid, kind=existing_kind
                )
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": vorher, "to": "superadmin", "sso": target.is_sso},
        )
    else:
        if target.role != "superadmin":
            return _admin_user_out(target)
        if await user_repo.count_superadmins(session) <= 1:
            raise ConflictError(
                "Der letzte Superadmin kann nicht herabgestuft werden.",
                code="cannot_demote_last_superadmin",
            )
        target.role = "admin"
        await audit.record(
            session,
            action=audit.USER_ROLE_CHANGED,
            actor=admin,
            target=target.username,
            request=request,
            detail={"from": "superadmin", "to": "admin", "sso": target.is_sso},
        )

    await session.commit()
    await session.refresh(target)
    return _admin_user_out(target)


@router.delete("/{user_id}", response_model=Message)
async def delete_user(
    request: Request, user: AdminUser, user_id: int, session: SessionDep
) -> Message:
    target = await user_repo.get(session, user_id)
    if target is None:
        raise NotFoundError("Benutzer nicht gefunden.", code="user_not_found")
    # Löschen gesperrt, wenn es nur einen Benutzer gibt.
    if await user_repo.count(session) <= 1:
        raise ConflictError("Der letzte Benutzer kann nicht gelöscht werden.", code="last_user")
    if target.id == user.id:
        raise ConflictError(
            "Sie können Ihr eigenes Konto nicht löschen.", code="cannot_delete_self"
        )
    # Superadmin-Ziel: NIE über einen Nicht-Superadmin-Aufrufer löschbar (Task 4,
    # Access-Modell/Superadmin-Phase -- Sicherheitsreview-Fix). Dieses Gate ist `AdminUser`,
    # nicht `SuperadminUser` -- ohne diese Prüfung könnte ein PLAIN Admin oder ein SSO-Admin
    # jeden NICHT-letzten Superadmin per Löschung entfernen, wiederholt bis zum letzten
    # (der Last-Superadmin-Schutz unten greift erst BEIM letzten). Analog zu `set_role`s
    # Schutz für den Rollenwechsel eines Superadmin-Ziels -- gleicher Fehlercode.
    if target.role == "superadmin" and (user.is_sso or user.role != "superadmin"):
        raise ForbiddenError(
            "Superadmin-Löschung nur durch einen Superadmin möglich.",
            code="superadmin_required",
        )
    # Cross-Tenant-Fix (Sicherheitsreview, Whole-Branch-Review Access-Modell/Superadmin-
    # Phase) -- analog zum Scope-Check in `set_role` oben: `delete_user` war nur über
    # `AdminUser` gegatet und löste `target` ohne RLS auf, ein lokaler Admin von Tenant A
    # konnte so einen NUR zu Tenant B gehörenden Benutzer löschen. Ein Superadmin-Aufrufer
    # überspringt diese Prüfung (voller Zugriff). Für jeden anderen Aufrufer muss die GESAMTE
    # Tenant-Zugehörigkeit des Ziels innerhalb der vom Aufrufer VERWALTETEN Tenants liegen
    # (Teilmengen-, nicht Schnittmengen-Regel -- sonst würde das Löschen eines auch-bei-B
    # zugewiesenen Kontos ungewollt Tenant B mittreffen, da `app_user` instanzweit ist). Ein
    # Ziel ganz ohne Tenant-Zugehörigkeit darf NUR ein Superadmin löschen.
    if user.is_sso or user.role != "superadmin":
        target_scope = await tenant_repo.allowed_tenant_ids(session, target)
        caller_admin_tenants = await tenant_repo.admin_tenants(session, user)
        if not target_scope or not target_scope <= caller_admin_tenants:
            raise ForbiddenError(
                "Konto ausserhalb des eigenen Kundenbereichs.", code="user_not_in_scope"
            )
    # Last-Superadmin-Schutz (Design §11.4) -- analog zum Last-Admin-Schutz oben, aber für
    # die instanzweite Rolle: ohne diese Prüfung könnte man den letzten Superadmin per
    # Löschung aussperren.
    if target.role == "superadmin" and await user_repo.count_superadmins(session) <= 1:
        raise ConflictError(
            "Der letzte Superadmin kann nicht gelöscht werden.",
            code="cannot_delete_last_superadmin",
        )
    await audit.record(
        session,
        action=audit.USER_DELETED,
        actor=user,
        target=target.username,
        request=request,
        detail={"role": target.role, "sso": target.is_sso},
    )
    await session.commit()
    # Carry-forward-Fix aus Task 1: `user_token.created_by` hat KEIN `ON DELETE` (ein
    # gelöschtes Erstellerkonto darf ein noch gültiges Token eines ANDEREN Nutzers nicht
    # mitreissen) -- ohne diesen Schritt VOR dem eigentlichen Löschen scheitert es mit
    # einem `IntegrityError`, sobald `target` noch offene, selbst ausgestellte Tokens hat
    # (z. B. eine von ihm verschickte Einladung/ein Reset-Link). Mirror der Sessions-
    # Löschung, die `user_repo.delete` bereits intern für die Tokens des GELÖSCHTEN Kontos
    # selbst übernimmt (kaskadiert über `app_user_id`, dafür nicht nötig).
    await user_token_repo.delete_created_by(session, user_id)
    await user_repo.delete(session, user_id)
    return Message(message="Benutzer gelöscht.")


@router.post("/sso/sync", response_model=Message)
async def sync_sso(_: AdminUser, session: SessionDep) -> Message:
    """Gleicht SSO-Benutzer PRO aktivem Mandanten ab -- jeder Kunde hat seine eigene
    ``oidc.admin_group_id``/``oidc.auditor_group_id``/``graph.*``-Konfiguration
    (Phase-3-TODO, hier geschlossen): vormals lief der Abgleich EINMAL auf der
    Owner-Session -- weil RLS für die Owner-Rolle nicht greift, läse ``get_all()`` dort ein
    undefiniertes Gemisch der ``oidc.*``-Zeilen ALLER Tenants, sobald ein zweiter existiert.

    ``app_user`` ist instanzweit (kein RLS) -- der eigentliche Schreibzugriff
    (``oidc.sync_sso_users``) läuft deshalb bewusst auf der übergebenen Owner-`session`
    (kein aktiver Tenant-Kontext an dieser Stelle: `tenant_scoped_session` bindet den
    Kontext nur für die Dauer seines eigenen `async with`-Blocks, s.u., danach ist der
    Owner-Kontext automatisch wieder aktiv) -- anders als der Hintergrund-Lauf
    (`runner.execute_run`), dessen Tenant-Schleife bereits INNERHALB eines aktiven
    `use_tenant`-Blocks steht und deshalb explizit `use_owner_context()` braucht.
    """
    from ...services import oidc

    tenants = await tenant_repo.list_active(session)
    configured = False
    synced = removed = 0
    blocked_tenants: list[str] = []
    for tenant in tenants:
        assert tenant.id is not None  # persistierte Zeile aus der DB
        async with tenant_scoped_session(tenant.id) as tsession:
            settings = await SettingsService(tsession).get_all()
        if not settings.get("oidc.enabled") or not settings.get("oidc.admin_group_id"):
            continue
        configured = True
        stats = await oidc.sync_sso_users(session, settings, tenant_id=tenant.id)
        synced += stats["synced"]
        removed += stats["removed"]
        if stats.get("removal_blocked"):
            blocked_tenants.append(tenant.name)

    if not configured:
        raise ConflictError(
            "SSO ist nicht aktiviert oder keine Admin-Gruppe hinterlegt.", code="sso_not_configured"
        )
    message = f"{synced} SSO-Benutzer synchronisiert, {removed} entfernt."
    if blocked_tenants:
        message += (
            f" Entfernen blockiert für: {', '.join(blocked_tenants)} (Schutz vor Aussperrung)."
        )
    return Message(message=message)


@router.get("/{user_id}/avatar")
async def get_user_avatar(_: AdminUser, user_id: int) -> FileResponse:
    """Profilbild EINES Kontos für die Access-Seite (Task B) -- Pendant zu `auth.py`s
    `GET /auth/me/avatar`, aber admin-facing (beliebiges `user_id`, nicht nur der
    Aufrufer selbst). Gate ist bewusst `AdminUser` (jeder Admin/Superadmin) statt einer
    tenant-gescopten Prüfung: die Access-Seite selbst scopt bereits, wer welche Konten
    überhaupt zu sehen bekommt (`list_users` oben); ein rein lesendes Bild-Serving ohne
    jede weitere Kontodaten-Preisgabe rechtfertigt keine zusätzliche Tenant-Prüfung hier.
    Kein Graph-Abgleich -- die Datei liegt bereits lokal gecacht (SSO-Login-Cache bzw.
    Selbst-Upload), diese Route liest nur.

    `Cache-Control: max-age=3600`: anders als `auth.py`s `/me/avatar` (dort `no-cache`,
    weil ein Selbst-Upload sofort sichtbar sein soll) trägt die URL hier IMMER
    `avatar_version` als Cache-Buster-Query (`?v=...`, s. `access.tsx`) -- eine neue
    Version bekommt automatisch eine neue URL, ein langes Caching der alten URL ist also
    gefahrlos und entlastet die Access-Seite bei vielen Konten."""
    if _avatar_mtime(user_id) is None:
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    return FileResponse(
        _avatar_path(user_id), media_type="image/png", headers={"Cache-Control": "max-age=3600"}
    )
