"""FastAPI-Dependencies: DB-Session, Auth-Guard, Settings, Rate-Limiter, Cookies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

import jwt
from fastapi import Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.errors import AuthError, ForbiddenError
from ..core.security import TokenPair, decode_token
from ..db.session import get_session, get_session_factory
from ..db.tenant_context import tenant_scoped_session
from ..models.user import AppUser
from ..repositories import tenant_repo, user_repo
from ..services.settings_service import SettingsService

ACCESS_COOKIE = "pwnotify_access"
REFRESH_COOKIE = "pwnotify_refresh"
TWOFA_COOKIE = "pwnotify_2fa"
OIDC_FLOW_COOKIE = "pwnotify_oidc_flow"

limiter = Limiter(key_func=get_remote_address)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_settings_service(session: SessionDep) -> SettingsService:
    return SettingsService(session)


SettingsDep = Annotated[SettingsService, Depends(get_settings_service)]

# Cache für die Default-Tenant-Id: die Zeile wird von der Phase-1-Migration einmalig
# angelegt (`is_default=true`) und ihre IDENTITÄT ändert sich nie mehr -- nur ihr Slug kann
# umbenannt werden -- ein Modul-Cache erspart bei jedem Lookup eine eigene Owner-Session.
# Wird noch für zwei Dinge gebraucht: die öffentlichen (unauthentifizierten) Branding-Routen
# (`get_public_tenant_session`, unten) und als Fallback-Tenant für den lokalen Admin in
# `tenant_repo.resolve_initial_tenant`.
_default_tenant_id_cache: int | None = None


async def default_tenant_id(session: AsyncSession) -> int:
    """Id des Default-Tenants (`tenant.is_default = true`), gecached -- der Cache bleibt auch
    nach einer Slug-Umbenennung gültig, da `is_default` die Identität trägt, nicht der Slug."""
    global _default_tenant_id_cache
    if _default_tenant_id_cache is None:
        tid = (await session.execute(text("SELECT id FROM tenant WHERE is_default"))).scalar_one()
        _default_tenant_id_cache = int(tid)
    return _default_tenant_id_cache


async def get_public_tenant_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI-Dependency: tenant-scoped Session für ÖFFENTLICHE (unauthentifizierte)
    Branding-Routen (Logo/Favicon/Theming auf der Login-Seite, vor jeder Anmeldung).

    Es gibt hier keinen Benutzer, den man autorisieren könnte -- bewusst immer der
    Default-Tenant. Nur für reines Theming gedacht, NICHT für Kundendaten (siehe
    `get_tenant_session` weiter unten für den authentifizierten, autorisierten Pfad).
    """
    async with get_session_factory()() as owner:
        tid = await default_tenant_id(owner)
    async with tenant_scoped_session(tid) as session:
        yield session


PublicTenantSessionDep = Annotated[AsyncSession, Depends(get_public_tenant_session)]


async def get_public_tenant_settings_service(session: PublicTenantSessionDep) -> SettingsService:
    return SettingsService(session)


PublicTenantSettingsDep = Annotated[SettingsService, Depends(get_public_tenant_settings_service)]


async def get_current_user(request: Request, session: SessionDep) -> AppUser:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise AuthError("Nicht angemeldet.", code="not_authenticated")
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Sitzung abgelaufen.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Ungültiges Token.", code="invalid_token") from exc
    user = await user_repo.get(session, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthError("Konto nicht verfügbar.", code="account_unavailable")
    # Revocation check (Task 2, L1): the user row is already loaded above, so this is a
    # plain field access -- zero extra DB roundtrips. A missing `gen` claim (token issued
    # before this feature shipped) defaults to 0, matching the default-0 column, so
    # existing tokens in the wild stay valid until they naturally expire.
    if payload.get("gen", 0) != user.token_generation:
        raise AuthError("Sitzung ungültig.", code="token_revoked")
    return user


CurrentUser = Annotated[AppUser, Depends(get_current_user)]


async def _claimed_active_tenant(request: Request) -> int | None:
    """Liest den `active_tenant`-Claim aus dem Access-Token, falls vorhanden.

    `get_current_user` (als Abhängigkeit dieser Funktion, siehe `get_tenant_session`) hat
    das Token bereits erfolgreich dekodiert -- ein erneuter Fehlschlag hier wäre eine echte
    Anomalie (z. B. Ablauf exakt zwischen beiden Dependency-Aufrufen). Fail-safe: in diesem
    Randfall gilt der Claim als nicht vorhanden, `get_tenant_session` fällt dann auf die
    `resolve_initial_tenant`-Neuauflösung zurück (weiterhin autorisiert, kein Leck) statt
    mit einem unerklärten 500 zu scheitern.
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.PyJWTError:
        return None
    raw = payload.get("active_tenant")
    return int(raw) if raw is not None else None


ActiveTenantClaim = Annotated[int | None, Depends(_claimed_active_tenant)]
"""Der rohe `active_tenant`-Claim aus dem Access-Token, unautorisiert -- nur zur ANZEIGE
(z. B. `UserOut.active_tenant`), NICHT zum Scopen von Kundendaten-Zugriffen (dafür immer
`get_tenant_session`/`TenantSessionDep`, die zusätzlich über `tenant_repo.is_allowed` gated)."""


async def _resolve_authorized_tenant(
    request: Request, user: AppUser, session: AsyncSession, *, write: bool = False
) -> int:
    """Löst den aktiven Mandanten auf und autorisiert ihn -- geteilte Logik zwischen
    `get_tenant_session`/`get_tenant_session_write` (Kundendaten) und `get_audit_session`
    (Audit-Protokoll für mandantengebundene Konten), damit alle exakt denselben autorisierten
    Tenant ermitteln.

    Liest den `active_tenant`-Claim des Access-Tokens und autorisiert ihn IMMER über
    `tenant_repo.is_allowed` -- auch wenn RLS einen fremden Tenant ohnehin leer liefern
    würde, wird hier schon verweigert (403). Damit scheitert ein gefälschter/veralteter
    Claim (z. B. ein zwischenzeitlich deaktivierter eigener Tenant, oder ein fremder Tenant
    in einem manipulierten Token) NIE stillschweigend mit 0 Zeilen, sondern immer explizit.

    `write` (Task 4, H8) passes the gate through to `tenant_repo.is_allowed`: `write=True`
    requires membership in `admin_tenants(user)` (write capability), `write=False`
    (default) only in `allowed_tenant_ids(user)` (admin OR auditor capability, read-only).
    Without this distinction, EVERY tenant-scoped route -- including write routes -- was
    previously authorized with the read gate, so an account with only an
    `auditor_tenant` grant (e.g. a stale assignment row left over from an
    auditor->admin role change) could reach write/action routes.

    Kein Claim (älteres Token, oder noch nie gesetzt): der Tenant wird wie beim Login neu
    aufgelöst (`resolve_initial_tenant`) und ebenso über `is_allowed` gegengeprüft. Liefert
    das keinen gültigen Tenant, ist das ein hartes 403 -- KEIN stiller Fallback auf den
    Default-Tenant, sonst sähe z. B. ein Auditor ohne Zuweisung fremde Kundendaten.

    `user`/`session` laufen auf der Owner-Rolle (kein RLS-Rollenwechsel) -- `tenant`,
    `app_user` und `auditor_tenant` sind instanzweite Tabellen, genau das braucht
    `tenant_repo` für seine Prüfungen.
    """
    claim_tid = await _claimed_active_tenant(request)
    if claim_tid is not None:
        if not await tenant_repo.is_allowed(session, user, claim_tid, write=write):
            raise ForbiddenError("Kein Zugriff auf diesen Mandanten.", code="tenant_forbidden")
        return claim_tid

    resolved = await tenant_repo.resolve_initial_tenant(session, user)
    if resolved is None or not await tenant_repo.is_allowed(session, user, resolved, write=write):
        raise ForbiddenError("Diesem Konto ist kein Mandant zugeordnet.", code="tenant_forbidden")
    return resolved


async def get_tenant_session(
    request: Request, user: CurrentUser, session: SessionDep
) -> AsyncGenerator[AsyncSession]:
    """FastAPI-Dependency: tenant-scoped Session für authentifizierte Kundendaten-Routen.

    Autorisierung/Auflösung siehe `_resolve_authorized_tenant`. Erst danach wird die
    tenant-gescopte Session (App-Rolle + GUC) geöffnet.
    """
    tid = await _resolve_authorized_tenant(request, user, session)
    async with tenant_scoped_session(tid) as scoped:
        yield scoped


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_audit_session(
    request: Request, user: CurrentUser, session: SessionDep
) -> AsyncGenerator[AsyncSession]:
    """FastAPI-Dependency für die Audit-Leserouten (`/audit`, `/audit/actions`).

    Sicherheitsgrenze (Whole-Branch-Review, Fix 1): `audit_log` ist eine RLS-tenant-gescopte
    Tabelle -- die Owner-Rolle umgeht RLS vollständig. Liefe die Route immer auf der
    Owner-Session, könnte ein SSO-Admin, gebunden an Tenant B, das GESAMTE Protokoll aller
    Mandanten lesen (Cross-Tenant-Offenlegung).

    Access-Modell/Superadmin-Design §2 (verschärft ggü. dem alten Drei-Wege-Modell): NUR der
    lokale SUPERADMIN (`not is_sso`, `role == "superadmin"`) ist instanzweit -- dafür bleibt
    es bei der Owner-Session (kein RLS-Rollenwechsel). Jedes andere Konto, EINSCHLIESSLICH
    des lokalen Admins (der jetzt auf seine `admin_tenant`-Zuweisungen beschränkt ist), jedes
    SSO-Konto und jeder lokale Auditor sieht NUR sein autorisiertes aktives Mandanten-
    Protokoll -- dieselbe Auflösung/Autorisierung wie bei Kundendaten
    (`_resolve_authorized_tenant`), RLS-scoped.
    """
    if not user.is_sso and user.role == "superadmin":
        yield session
        return

    tid = await _resolve_authorized_tenant(request, user, session)
    async with tenant_scoped_session(tid) as scoped:
        yield scoped


AuditSessionDep = Annotated[AsyncSession, Depends(get_audit_session)]


async def get_tenant_settings_service(session: TenantSessionDep) -> SettingsService:
    return SettingsService(session)


TenantSettingsDep = Annotated[SettingsService, Depends(get_tenant_settings_service)]


async def get_tenant_session_write(
    request: Request, user: CurrentUser, session: SessionDep
) -> AsyncGenerator[AsyncSession]:
    """Like `get_tenant_session`, but with the WRITE gate (Task 4, H8): authorized via
    `_resolve_authorized_tenant(..., write=True)`, requiring membership in
    `admin_tenants(user)` (or superadmin) instead of the mere read assignment.

    For the tenant-scoped WRITE/action routes (settings changes, exclusions, retry,
    immediate reminder, bulk actions, `/runs/trigger`) -- an account with only an
    `auditor_tenant` grant must reach NOTHING through this, even if it passes the
    `AdminUser` role gate (e.g. due to a stale assignment row)."""
    tid = await _resolve_authorized_tenant(request, user, session, write=True)
    async with tenant_scoped_session(tid) as scoped:
        yield scoped


TenantWriteSessionDep = Annotated[AsyncSession, Depends(get_tenant_session_write)]


async def get_tenant_settings_service_write(session: TenantWriteSessionDep) -> SettingsService:
    return SettingsService(session)


TenantWriteSettingsDep = Annotated[SettingsService, Depends(get_tenant_settings_service_write)]


async def get_enrolling_user(request: Request, session: SessionDep) -> AppUser:
    """Benutzer aus der normalen Sitzung ODER aus dem 2FA-Zwischentoken.

    Nur für die 2FA-Einrichtung. Bei aktiver 2FA-Pflicht stellt der Login bewusst noch
    keine Sitzung aus — ohne diesen Weg käme man nicht mehr an die Einrichtung heran und
    sässe fest. Der Zwischentoken lebt 5 Minuten und erlaubt ausschliesslich Einrichten
    und Aktivieren; alle anderen Endpunkte verlangen weiterhin ein Access-Token.
    """
    if request.cookies.get(ACCESS_COOKIE):
        return await get_current_user(request, session)

    token = request.cookies.get(TWOFA_COOKIE)
    if not token:
        raise AuthError("Nicht angemeldet.", code="not_authenticated")
    try:
        payload = decode_token(token, expected_type="2fa")
    except jwt.PyJWTError as exc:
        raise AuthError(
            "2FA-Sitzung abgelaufen. Bitte erneut anmelden.", code="twofa_expired"
        ) from exc
    user = await user_repo.get(session, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthError("Konto nicht verfügbar.", code="account_unavailable")
    return user


EnrollingUser = Annotated[AppUser, Depends(get_enrolling_user)]


async def require_admin(user: CurrentUser) -> AppUser:
    """Schreibzugriff nur für Admins. Auditoren (read-only) -> 403.

    Der Superadmin MUSS jedes Admin-Gate passieren -- er ist die Rolle mit den WEITESTEN
    Rechten, nicht mit weniger (`role != "admin"` hätte ihn hier fälschlich ausgesperrt)."""
    if user.role not in ("admin", "superadmin"):
        raise ForbiddenError("Nur mit Administratorrechten möglich.", code="admin_required")
    return user


AdminUser = Annotated[AppUser, Depends(require_admin)]


def is_superadmin(user: AppUser) -> bool:
    """A local superadmin -- instance-wide authority (not SSO, role == "superadmin").
    Shared predicate for routes that keep instance-wide behaviour superadmin-exclusive."""
    return not user.is_sso and user.role == "superadmin"


async def require_local_admin(user: CurrentUser) -> AppUser:
    """Lokale (nicht-SSO) Administration -- Admin ODER Superadmin, aber kein SSO-Konto.

    Mandantengebundene SSO-Konten (auch role==admin) und lokale Auditoren dürfen hierüber
    nicht rein -- dieselbe Grenze wie in `get_audit_session`. Tenant-CRUD selbst ist NICHT
    mehr über dieses Gate geschützt (siehe `require_superadmin`/`admin_tenants.py`), dieses
    Gate bleibt für andere lokale-Admin-Routen bestehen.
    """
    if user.is_sso or user.role not in ("admin", "superadmin"):
        raise ForbiddenError(
            "Nur mit lokalen Administratorrechten möglich.", code="local_admin_required"
        )
    return user


LocalAdminUser = Annotated[AppUser, Depends(require_local_admin)]


async def require_superadmin(user: CurrentUser) -> AppUser:
    """Instanzweite Verwaltung (Kunden-CRUD, alle Zuweisungen, Superadmin-Verwaltung,
    Multi-Tenant-Mode-Schalter) NUR für den lokalen Superadmin (Design §4/§6).

    Jedes andere Konto -- inklusive eines lokalen Admins, der jetzt auf seine
    `admin_tenant`-Zuweisungen beschränkt ist -- darf keine Kunden anlegen/ändern/löschen
    oder Zuweisungen setzen."""
    if user.is_sso or user.role != "superadmin":
        raise ForbiddenError(
            "Nur mit Superadministratorrechten möglich.", code="superadmin_required"
        )
    return user


SuperadminUser = Annotated[AppUser, Depends(require_superadmin)]


async def require_superadmin_default_context(
    request: Request, user: SuperadminUser, session: SessionDep
) -> AppUser:
    """Wie `require_superadmin`, aber zusätzlich nur im DEFAULT-Kontext (Context-Gating v2,
    Design §4/§4-notes, Matrix B).

    Schaltet der Superadmin in einen Kunden-Kontext um, sieht er dessen operative Sicht wie
    ein Kunden-Admin -- Instanz-Einstellungen (Mode-Schalter + Default-Umbenennung), die
    Mandanten-Konsole (CRUD) und die Zuweisungs-Konsole sind Provider-Ebene-Aktionen, die NUR
    aus dem Default-Kontext heraus erlaubt sind. Das Frontend blendet sie dort aus (Task 5);
    dieses Gate ist die Backend-Verteidigungslinie dahinter, damit eine manipulierte Anfrage
    (z. B. direkt gegen die API, mit einem `active_tenant`-Claim auf einen Kundentenant) das
    nicht umgehen kann.

    `_resolve_authorized_tenant` liefert bei einem Superadmin OHNE `active_tenant`-Claim
    (z. B. ein frisch ausgestelltes Token vor dem ersten Umschalten) über
    `tenant_repo.resolve_initial_tenant` den Default-Tenant zurück -- der Default-Kontext ist
    damit der natürliche Ausgangszustand, kein Sonderfall, den man hier gesondert behandeln
    müsste."""
    active = await _resolve_authorized_tenant(request, user, session)
    if active != await default_tenant_id(session):
        raise ForbiddenError("Nur im Standard-Kontext möglich.", code="default_context_required")
    return user


SuperadminDefaultContextUser = Annotated[AppUser, Depends(require_superadmin_default_context)]


async def superadmin_in_default_context(
    request: Request, user: AppUser, session: AsyncSession
) -> bool:
    """Non-raising counterpart to ``require_superadmin_default_context``: True only for a local
    superadmin whose active tenant is the default (provider) tenant.

    For routes that stay open to EVERY account but must expose a provider-only field ONLY in
    that context -- e.g. ``GET /admin/instance`` returns ``multi_tenant_mode`` to all accounts
    (UI gating) but ``default_tenant_name`` (provider metadata) only here (I5). A superadmin
    switched into a customer context sees the customer's operative view, so the provider name
    is withheld there too, matching Matrix B and the PUT gate above.
    """
    if not is_superadmin(user):
        return False
    active = await _resolve_authorized_tenant(request, user, session)
    return active == await default_tenant_id(session)


def _cookie_kwargs() -> dict[str, object]:
    settings = get_settings()
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": settings.cookie_secure,
        "path": "/",
    }


def set_auth_cookies(response: Response, pair: TokenPair) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE,
        pair.access_token,
        max_age=settings.access_token_ttl_min * 60,
        **_cookie_kwargs(),  # type: ignore[arg-type]
    )
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        **_cookie_kwargs(),  # type: ignore[arg-type]
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


def set_2fa_cookie(response: Response, token: str) -> None:
    response.set_cookie(TWOFA_COOKIE, token, max_age=300, **_cookie_kwargs())  # type: ignore[arg-type]


def clear_2fa_cookie(response: Response) -> None:
    response.delete_cookie(TWOFA_COOKIE, path="/")


def set_oidc_flow_cookie(response: Response, value: str) -> None:
    """Short-lived, browser-bound carrier for the encrypted MSAL auth-code flow dict.

    NOT routed through `_cookie_kwargs()`: that hardcodes `samesite="strict"`/`path="/"`, which
    would break every SSO login.

    With `response_mode=form_post` (RFC 9700 §4.3.1) the callback is now a cross-site *POST*
    submitted by an auto-posting form on `login.microsoftonline.com`, not a top-level GET
    navigation. A browser does NOT attach a `SameSite=Lax` cookie to a cross-site POST, so the
    flow cookie (PKCE verifier + nonce + state) would never reach the callback and every SSO
    login would break. It must therefore be `SameSite=None`, and the cookie spec mandates that
    `SameSite=None` always be paired with `Secure`. `secure=True` is hardcoded here (NOT
    `settings.cookie_secure`): `Secure` is now a hard requirement of the flow, which is the
    deliberately accepted consequence that SSO works only over HTTPS. Only THIS cookie changes;
    the access/refresh/2fa cookies keep their strict, first-party settings.
    """
    response.set_cookie(
        OIDC_FLOW_COOKIE,
        value,
        max_age=600,
        httponly=True,
        samesite="none",
        secure=True,
        path="/api/auth/oidc",
    )


def clear_oidc_flow_cookie(response: Response) -> None:
    response.delete_cookie(OIDC_FLOW_COOKIE, path="/api/auth/oidc")
