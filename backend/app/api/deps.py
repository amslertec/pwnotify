"""FastAPI-Dependencies: DB-Session, Auth-Guard, Settings, Rate-Limiter, Cookies."""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.errors import AuthError, ForbiddenError
from ..core.security import TokenPair, decode_token
from ..db.session import get_session
from ..models.user import AppUser
from ..repositories import user_repo
from ..services.settings_service import SettingsService

ACCESS_COOKIE = "pwnotify_access"
REFRESH_COOKIE = "pwnotify_refresh"
TWOFA_COOKIE = "pwnotify_2fa"

limiter = Limiter(key_func=get_remote_address)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_settings_service(session: SessionDep) -> SettingsService:
    return SettingsService(session)


SettingsDep = Annotated[SettingsService, Depends(get_settings_service)]


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
