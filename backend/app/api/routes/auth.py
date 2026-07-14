"""Lokale Authentifizierung (JWT-Cookies, Refresh-Rotation, Brute-Force-Schutz)."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import uuid
from pathlib import Path

import jwt
from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from ...core.config import get_settings
from ...core.crypto import decrypt, encrypt
from ...core.errors import AuthError, NotFoundError, PwNotifyError
from ...core.security import (
    create_2fa_token,
    decode_token,
    hash_password,
    hash_token,
    issue_token_pair,
    needs_rehash,
    verify_password,
)
from ...core.twofa import (
    generate_recovery_codes,
    generate_secret,
    match_recovery_code,
    provisioning_uri,
    qr_png_data_uri,
    verify_totp,
)
from ...models._base import utcnow
from ...models.user import AppUser
from ...repositories import user_repo
from ...schemas.auth import (
    LanguageUpdate,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    ProfileUpdate,
    RecoveryCodesOut,
    SessionOut,
    TwoFactorCode,
    TwoFactorSetupOut,
    UserOut,
)
from ...schemas.common import Message
from ...services import oidc
from ...services.graph import GraphClient, GraphConfig
from ...services.settings_service import SettingsService, effective_base_url
from ..deps import (
    REFRESH_COOKIE,
    TWOFA_COOKIE,
    CurrentUser,
    SessionDep,
    clear_2fa_cookie,
    clear_auth_cookies,
    limiter,
    set_2fa_cookie,
    set_auth_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()

_AVATAR_ALLOWED = {"image/png", "image/jpeg", "image/webp"}
_AVATAR_MAX_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Profilbild (lokal hochgeladen oder aus Entra gecacht) — als quadratisches PNG
# --------------------------------------------------------------------------- #
def _avatar_dir() -> Path:
    d = Path(_settings.data_dir) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _avatar_path(user_id: int) -> Path:
    return _avatar_dir() / f"{user_id}.png"


def _process_avatar(data: bytes) -> bytes | None:
    """Bild zentriert quadratisch zuschneiden -> 256x256 PNG. None bei Fehler."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize(
            (256, 256), Image.Resampling.LANCZOS
        )
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # ungültige/kaputte Bilddatei
        return None


async def _cache_sso_avatar(user_id: int, upn: str, settings: dict[str, object]) -> None:
    """Profilfoto des SSO-Benutzers aus Entra holen und als Avatar cachen (best effort)."""
    if not settings.get("graph.client_secret"):
        return
    graph = GraphClient(
        GraphConfig(
            tenant_id=str(settings.get("graph.tenant_id") or ""),
            client_id=str(settings.get("graph.client_id") or ""),
            client_secret=str(settings.get("graph.client_secret") or ""),
            cloud=str(settings.get("graph.cloud") or "global"),
        )
    )
    raw = await graph.get_user_photo(upn)
    if not raw:
        return
    processed = _process_avatar(raw)
    if processed is not None:
        _avatar_path(user_id).write_bytes(processed)


def _user_out(user: AppUser) -> UserOut:
    """UserOut inkl. Avatar-Status (Existenz + mtime als Cache-Buster)."""
    path = _avatar_path(user.id) if user.id is not None else None
    exists = bool(path and path.exists())
    return UserOut(
        id=user.id,  # type: ignore[arg-type]
        username=user.username,
        display_name=user.display_name,
        is_sso=user.is_sso,
        role=user.role,
        language=user.language,
        two_factor_enabled=user.totp_enabled,
        last_login_at=user.last_login_at,
        has_avatar=exists,
        avatar_version=int(path.stat().st_mtime) if exists and path else 0,
    )


async def _complete_login(
    request: Request, response: Response, session: SessionDep, user: AppUser
) -> LoginResponse:
    """Volltoken ausstellen + Sitzung anlegen (nach Passwort bzw. 2FA-Code)."""
    user.last_login_at = utcnow()
    await user_repo.prune_sessions(session, user.id)  # type: ignore[arg-type]
    pair = issue_token_pair(str(user.id))
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    await session.commit()
    set_auth_cookies(response, pair)
    return LoginResponse(two_factor_required=False, user=_user_out(user))


@router.post("/login", response_model=LoginResponse)
@limiter.limit(_settings.login_rate_limit)
async def login(
    request: Request, response: Response, body: LoginRequest, session: SessionDep
) -> LoginResponse:
    user = await user_repo.get_by_username(session, body.username)
    now = utcnow()

    if user and user.locked_until and user.locked_until > now:
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )

    if user is None or not verify_password(body.password, user.password_hash):
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= _settings.login_max_failures:
                user.locked_until = now + dt.timedelta(minutes=_settings.login_lockout_min)
                user.failed_login_count = 0
            await session.commit()
        raise AuthError("Ungültiger Benutzername oder Passwort.", code="invalid_credentials")

    # Passwort ok
    user.failed_login_count = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)

    # 2FA aktiv -> Zwischenschritt: kurzlebiger 2fa-Cookie, noch keine volle Sitzung.
    if user.totp_enabled and user.totp_secret:
        await session.commit()
        set_2fa_cookie(response, create_2fa_token(str(user.id)))
        return LoginResponse(two_factor_required=True)

    return await _complete_login(request, response, session, user)


@router.post("/2fa/verify", response_model=LoginResponse)
@limiter.limit(_settings.login_rate_limit)
async def two_factor_verify(
    request: Request, response: Response, body: TwoFactorCode, session: SessionDep
) -> LoginResponse:
    token = request.cookies.get(TWOFA_COOKIE)
    if not token:
        raise AuthError("Keine 2FA-Sitzung. Bitte erneut anmelden.", code="twofa_session_missing")
    try:
        payload = decode_token(token, expected_type="2fa")
    except jwt.PyJWTError as exc:
        clear_2fa_cookie(response)
        raise AuthError(
            "2FA-Sitzung abgelaufen. Bitte erneut anmelden.", code="twofa_expired"
        ) from exc

    user = await user_repo.get(session, int(payload["sub"]))
    if user is None or not user.is_active or not user.totp_enabled or not user.totp_secret:
        clear_2fa_cookie(response)
        raise AuthError("Konto nicht verfügbar.", code="account_unavailable")

    ok = verify_totp(decrypt(user.totp_secret), body.code)
    if not ok:
        # Recovery-Code als Fallback (verbraucht ihn).
        hashes: list[str] = json.loads(user.recovery_codes or "[]")
        matched = match_recovery_code(body.code, hashes)
        if matched:
            hashes.remove(matched)
            user.recovery_codes = json.dumps(hashes)
            ok = True
    if not ok:
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")

    clear_2fa_cookie(response)
    return await _complete_login(request, response, session, user)


@router.post("/refresh", response_model=UserOut)
async def refresh(request: Request, response: Response, session: SessionDep) -> UserOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthError("Kein Refresh-Token.", code="no_refresh_token")
    try:
        payload = decode_token(token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        clear_auth_cookies(response)
        raise AuthError("Ungültiges Refresh-Token.", code="invalid_token") from exc

    us = await user_repo.get_session_by_jti(session, payload["jti"])
    now = utcnow()
    # Reuse-/Diebstahl-Erkennung: bekanntes, aber revoked/abweichendes Token -> alles sperren.
    if us is None or us.revoked or us.token_hash != hash_token(token) or us.expires_at < now:
        if us is not None:
            await user_repo.revoke_all(session, us.user_id)
        clear_auth_cookies(response)
        raise AuthError("Sitzung ungültig. Bitte erneut anmelden.", code="session_invalid")

    user = await user_repo.get(session, us.user_id)
    if user is None or not user.is_active:
        clear_auth_cookies(response)
        raise AuthError("Konto nicht verfügbar.", code="account_unavailable")

    # Rotation in-place: dieselbe Sitzung behält eine Zeile, Token wird ausgetauscht.
    pair = issue_token_pair(str(user.id))
    us.refresh_jti = pair.refresh_jti
    us.token_hash = hash_token(pair.refresh_token)
    us.expires_at = pair.refresh_expires
    us.last_used_at = now
    us.user_agent = request.headers.get("user-agent") or us.user_agent
    us.ip_address = (request.client.host if request.client else None) or us.ip_address
    await session.commit()
    set_auth_cookies(response, pair)
    return _user_out(user)


@router.post("/logout", response_model=Message)
async def logout(request: Request, response: Response, session: SessionDep) -> Message:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, expected_type="refresh")
            await user_repo.revoke_session(session, payload["jti"])
        except jwt.PyJWTError:
            pass
    clear_auth_cookies(response)
    return Message(message="Abgemeldet.")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return _user_out(user)


@router.post("/profile", response_model=UserOut)
async def update_profile(body: ProfileUpdate, user: CurrentUser, session: SessionDep) -> UserOut:
    user.display_name = (body.display_name or "").strip() or None
    user.updated_at = utcnow()
    await session.commit()
    await session.refresh(user)
    return _user_out(user)


@router.post("/language", response_model=UserOut)
async def set_language(body: LanguageUpdate, user: CurrentUser, session: SessionDep) -> UserOut:
    user.language = body.language
    user.updated_at = utcnow()
    await session.commit()
    await session.refresh(user)
    return _user_out(user)


@router.get("/me/avatar")
async def get_my_avatar(user: CurrentUser) -> FileResponse:
    path = _avatar_path(user.id) if user.id is not None else None
    if not (path and path.exists()):
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.post("/me/avatar", response_model=UserOut)
async def upload_my_avatar(user: CurrentUser, file: UploadFile = File(...)) -> UserOut:
    if user.is_sso:
        raise PwNotifyError(
            "Das Profilbild wird aus Microsoft Entra übernommen.", code="avatar_sso_managed"
        )
    if (file.content_type or "") not in _AVATAR_ALLOWED:
        raise PwNotifyError(
            "Nicht unterstütztes Format (PNG, JPG, WebP).", code="unsupported_format"
        )
    data = await file.read()
    if len(data) > _AVATAR_MAX_BYTES:
        raise PwNotifyError("Datei zu gross (max. 5 MB).", code="file_too_large")
    processed = _process_avatar(data)
    if processed is None:
        raise PwNotifyError("Ungültige Bilddatei.", code="invalid_image")
    _avatar_path(user.id).write_bytes(processed)  # type: ignore[arg-type]
    return _user_out(user)


@router.delete("/me/avatar", response_model=UserOut)
async def delete_my_avatar(user: CurrentUser) -> UserOut:
    if user.is_sso:
        raise PwNotifyError(
            "Das Profilbild wird aus Microsoft Entra übernommen.", code="avatar_sso_managed"
        )
    if user.id is not None:
        _avatar_path(user.id).unlink(missing_ok=True)
    return _user_out(user)


@router.get("/sessions", response_model=list[SessionOut])
async def sessions(request: Request, user: CurrentUser, session: SessionDep) -> list[SessionOut]:
    current_jti = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        with contextlib.suppress(jwt.PyJWTError):
            current_jti = decode_token(token, expected_type="refresh").get("jti")
    rows = await user_repo.list_sessions(session, user.id)  # type: ignore[arg-type]
    return [
        SessionOut(
            id=s.id,  # type: ignore[arg-type]
            user_agent=s.user_agent,
            ip_address=s.ip_address,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            current=(s.refresh_jti == current_jti),
        )
        for s in rows
    ]


@router.post("/sessions/revoke-others", response_model=Message)
async def revoke_other_sessions(
    request: Request, user: CurrentUser, session: SessionDep
) -> Message:
    current_jti = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        with contextlib.suppress(jwt.PyJWTError):
            current_jti = decode_token(token, expected_type="refresh").get("jti")
    n = await user_repo.revoke_others(session, user.id, current_jti)  # type: ignore[arg-type]
    return Message(message=f"{n} andere Sitzung(en) abgemeldet.")


@router.post("/password", response_model=Message)
async def change_password(
    body: PasswordChangeRequest, user: CurrentUser, session: SessionDep
) -> Message:
    if not verify_password(body.current_password, user.password_hash):
        raise AuthError("Aktuelles Passwort ist falsch.", code="wrong_current_password")
    user.password_hash = hash_password(body.new_password)
    user.updated_at = utcnow()
    await session.commit()
    return Message(message="Passwort geändert.")


# --------------------------------------------------------------------------- #
# 2FA (TOTP) — Verwaltung (nur lokale Konten)
# --------------------------------------------------------------------------- #
@router.post("/2fa/setup", response_model=TwoFactorSetupOut)
async def two_factor_setup(user: CurrentUser, session: SessionDep) -> TwoFactorSetupOut:
    if user.is_sso:
        raise PwNotifyError("2FA ist nur für lokale Konten verfügbar.", code="twofa_local_only")
    secret = generate_secret()
    user.totp_secret = encrypt(secret)  # gespeichert, aber noch nicht aktiv
    user.totp_enabled = False
    await session.commit()
    uri = provisioning_uri(secret, user.username)
    return TwoFactorSetupOut(otpauth_uri=uri, qr_png=qr_png_data_uri(uri), secret=secret)


@router.post("/2fa/enable", response_model=RecoveryCodesOut)
async def two_factor_enable(
    body: TwoFactorCode, user: CurrentUser, session: SessionDep
) -> RecoveryCodesOut:
    if user.is_sso or not user.totp_secret:
        raise PwNotifyError("2FA-Einrichtung nicht gestartet.", code="twofa_not_started")
    if not verify_totp(decrypt(user.totp_secret), body.code):
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    codes, hashes = generate_recovery_codes()
    user.totp_enabled = True
    user.recovery_codes = json.dumps(hashes)
    user.updated_at = utcnow()
    await session.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/2fa/disable", response_model=UserOut)
async def two_factor_disable(
    body: TwoFactorCode, user: CurrentUser, session: SessionDep
) -> UserOut:
    if not user.totp_enabled:
        return _user_out(user)
    valid = (user.totp_secret and verify_totp(decrypt(user.totp_secret), body.code)) or (
        match_recovery_code(body.code, json.loads(user.recovery_codes or "[]")) is not None
    )
    if not valid:
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = None
    user.updated_at = utcnow()
    await session.commit()
    await session.refresh(user)
    return _user_out(user)


# --------------------------------------------------------------------------- #
# SSO / OIDC (Microsoft Entra)
# --------------------------------------------------------------------------- #
@router.get("/config")
async def auth_config(session: SessionDep) -> dict[str, object]:
    """Öffentlich: teilt der Login-Seite mit, ob SSO verfügbar ist (+ Button-Text)."""
    settings = await SettingsService(session).get_all()
    return {
        "oidc_enabled": oidc.is_configured(settings),
        "oidc_button_label": settings.get("oidc.button_label") or "Mit Microsoft anmelden",
    }


def _redirect_uri(base: str) -> str:
    return f"{base}/api/auth/oidc/callback"


@router.get("/oidc/login")
async def oidc_login(session: SessionDep, login_hint: str | None = None) -> RedirectResponse:
    settings = await SettingsService(session).get_all()
    base = effective_base_url(settings)
    url = oidc.build_login_url(
        settings, _redirect_uri(base), oidc.sign_state(), login_hint=login_hint
    )
    return RedirectResponse(url, status_code=302)


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    session: SessionDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = await SettingsService(session).get_all()
    base = effective_base_url(settings)
    if error or not code or not state:
        return RedirectResponse(f"{base}/login?sso_error=1", status_code=302)

    oidc.verify_state(state)
    result = await oidc.exchange_and_verify(settings, code, _redirect_uri(base))
    if not result.allowed or not result.username:
        return RedirectResponse(f"{base}/login?sso_denied=1", status_code=302)

    user = await user_repo.get_by_username(session, result.username)
    if user is None:
        # SSO-Nutzer ohne lokales Passwort (unbrauchbarer Zufalls-Hash).
        user = await user_repo.create(
            session,
            username=result.username,
            password_hash=hash_password(uuid.uuid4().hex),
            display_name=result.display_name,
            role=result.role,
            is_sso=True,
        )
    else:
        user.is_sso = True
        user.display_name = result.display_name
        user.role = result.role  # Rolle folgt der Entra-Gruppenmitgliedschaft
    user.last_login_at = utcnow()
    pair = issue_token_pair(str(user.id))
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    await session.commit()
    # Profilfoto aus Entra holen und cachen (best effort; blockiert Login nicht bei Fehler).
    if user.id is not None:
        with contextlib.suppress(Exception):
            await _cache_sso_avatar(user.id, result.username, settings)
    resp = RedirectResponse(f"{base}/", status_code=302)
    set_auth_cookies(resp, pair)
    return resp
