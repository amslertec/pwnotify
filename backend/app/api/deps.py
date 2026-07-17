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

limiter = Limiter(key_func=get_remote_address)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_settings_service(session: SessionDep) -> SettingsService:
    return SettingsService(session)


SettingsDep = Annotated[SettingsService, Depends(get_settings_service)]

# Cache für die Default-Tenant-Id: die Zeile wird von der Phase-1-Migration einmalig
# angelegt (`slug='default'`) und nie mehr geändert -- ein Modul-Cache erspart bei jedem
# Lookup eine eigene Owner-Session. Wird noch für zwei Dinge gebraucht: die öffentlichen
# (unauthentifizierten) Branding-Routen (`get_public_tenant_session`, unten) und als
# Fallback-Tenant für den lokalen Admin in `tenant_repo.resolve_initial_tenant`.
_default_tenant_id_cache: int | None = None


async def default_tenant_id(session: AsyncSession) -> int:
    """Id des Default-Tenants (`tenant.slug = 'default'`), gecached."""
    global _default_tenant_id_cache
    if _default_tenant_id_cache is None:
        tid = (
            await session.execute(text("SELECT id FROM tenant WHERE slug = 'default'"))
        ).scalar_one()
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


async def get_tenant_session(
    request: Request, user: CurrentUser, session: SessionDep
) -> AsyncGenerator[AsyncSession]:
    """FastAPI-Dependency: tenant-scoped Session für authentifizierte Kundendaten-Routen.

    Löst den aktiven Tenant aus dem `active_tenant`-Claim des Access-Tokens auf und
    autorisiert ihn IMMER über `tenant_repo.is_allowed` -- auch wenn RLS einen fremden
    Tenant ohnehin leer liefern würde, wird hier schon verweigert (403). Damit scheitert
    ein gefälschter/veralteter Claim (z. B. ein zwischenzeitlich deaktivierter eigener
    Tenant, oder ein fremder Tenant in einem manipulierten Token) NIE stillschweigend mit
    0 Zeilen, sondern immer explizit.

    Kein Claim (älteres Token, oder noch nie gesetzt): der Tenant wird wie beim Login neu
    aufgelöst (`resolve_initial_tenant`) und ebenso über `is_allowed` gegengeprüft. Liefert
    das keinen gültigen Tenant, ist das ein hartes 403 -- KEIN stiller Fallback auf den
    Default-Tenant, sonst sähe z. B. ein Auditor ohne Zuweisung fremde Kundendaten.

    `user`/`session` laufen auf der Owner-Rolle (kein RLS-Rollenwechsel) -- `tenant`,
    `app_user` und `auditor_tenant` sind instanzweite Tabellen, genau das braucht
    `tenant_repo` für seine Prüfungen. Erst danach wird die tenant-gescopte Session
    (App-Rolle + GUC) geöffnet.
    """
    claim_tid = await _claimed_active_tenant(request)
    if claim_tid is not None:
        if not await tenant_repo.is_allowed(session, user, claim_tid):
            raise ForbiddenError("Kein Zugriff auf diesen Mandanten.", code="tenant_forbidden")
        tid = claim_tid
    else:
        resolved = await tenant_repo.resolve_initial_tenant(session, user)
        if resolved is None or not await tenant_repo.is_allowed(session, user, resolved):
            raise ForbiddenError(
                "Diesem Konto ist kein Mandant zugeordnet.", code="tenant_forbidden"
            )
        tid = resolved

    async with tenant_scoped_session(tid) as scoped:
        yield scoped


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_tenant_settings_service(session: TenantSessionDep) -> SettingsService:
    return SettingsService(session)


TenantSettingsDep = Annotated[SettingsService, Depends(get_tenant_settings_service)]


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
    """Schreibzugriff nur für Admins. Auditoren (read-only) -> 403."""
    if user.role != "admin":
        raise ForbiddenError("Nur mit Administratorrechten möglich.", code="admin_required")
    return user


AdminUser = Annotated[AppUser, Depends(require_admin)]


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
