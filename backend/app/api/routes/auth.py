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

from ...core import imagetype
from ...core.config import get_settings
from ...core.crypto import decrypt, encrypt
from ...core.errors import AuthError, ForbiddenError, NotFoundError, PwNotifyError
from ...core.logging import get_logger
from ...core.security import (
    create_2fa_token,
    decode_token,
    hash_password,
    hash_token,
    issue_token_pair,
    needs_rehash,
    register_failed_attempt,
    reset_failed_attempts,
    verify_password,
)
from ...core.twofa import (
    generate_recovery_codes,
    generate_secret,
    match_recovery_code,
    matching_step,
    provisioning_uri,
    qr_png_data_uri,
    verify_totp,
)
from ...models._base import utcnow
from ...models.user import AppUser
from ...repositories import tenant_repo, user_repo
from ...schemas.auth import (
    LanguageUpdate,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    ProfileUpdate,
    RecoveryCodesOut,
    SessionOut,
    SwitchTenantRequest,
    TenantRef,
    TwoFactorCode,
    TwoFactorSetupOut,
    UserOut,
)
from ...schemas.common import Message
from ...services import audit, oidc
from ...services.graph import GraphClient, GraphConfig
from ...services.settings_service import SettingsService, effective_base_url
from ..deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    TWOFA_COOKIE,
    ActiveTenantClaim,
    CurrentUser,
    EnrollingUser,
    SessionDep,
    clear_2fa_cookie,
    clear_auth_cookies,
    limiter,
    set_2fa_cookie,
    set_auth_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()
log = get_logger("auth")

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


async def _user_out(session: SessionDep, user: AppUser, active_tenant_id: int | None) -> UserOut:
    """UserOut inkl. Avatar-Status (Existenz + mtime als Cache-Buster) und Mandanten-Info
    (Phase 4a Task 5): der aktive Mandant (aus Claim/Session, ungeprüft -- reine Anzeige,
    siehe `ActiveTenantClaim`-Docstring) sowie die Liste der Mandanten, zu denen dieses
    Konto umschalten darf (`tenant_repo.allowed_tenant_ids`: None -> alle aktiven, sonst
    genau diese, jeweils weiter auf tatsächlich AKTIVE Tenants beschränkt)."""
    path = _avatar_path(user.id) if user.id is not None else None
    exists = bool(path and path.exists())

    active_tenant: TenantRef | None = None
    if active_tenant_id is not None:
        t = await tenant_repo.get(session, active_tenant_id)
        if t is not None:
            active_tenant = TenantRef(id=t.id, name=t.name)  # type: ignore[arg-type]

    allowed_ids = await tenant_repo.allowed_tenant_ids(session, user)
    active_tenants = await tenant_repo.list_active(session)
    if allowed_ids is not None:
        active_tenants = [t for t in active_tenants if t.id in allowed_ids]
    switchable = [TenantRef(id=t.id, name=t.name) for t in active_tenants]  # type: ignore[arg-type]

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
        idle_timeout_min=_settings.idle_timeout_min,
        active_tenant=active_tenant,
        switchable_tenants=switchable,
    )


async def _complete_login(
    request: Request, response: Response, session: SessionDep, user: AppUser
) -> LoginResponse:
    """Volltoken ausstellen + Sitzung anlegen (nach Passwort bzw. 2FA-Code)."""
    await audit.record(
        session,
        action=audit.LOGIN_SUCCESS,
        actor=user,
        request=request,
        detail={"sso": user.is_sso, "two_factor": user.totp_enabled},
    )
    user.last_login_at = utcnow()
    await user_repo.prune_sessions(session, user.id)  # type: ignore[arg-type]

    # Aktiven Tenant fürs Login bestimmen -- IMMER gegen `is_allowed` gegenprüfen, nicht
    # `resolve_initial_tenant` blind übernehmen: die Auflösung dort gated NICHT auf
    # is_active (siehe deren Docstring), sonst käme z. B. ein inzwischen deaktivierter,
    # eigener SSO-Tenant als aktiver Claim ins Token/`active_tenant_id` -- eine
    # Verfügbarkeitslücke, kein Cross-Tenant-Leck, aber trotzdem falsch. Uniform für alle
    # Kontoarten, kein Sonderfall.
    tid = await tenant_repo.resolve_initial_tenant(session, user)
    if tid is not None and not await tenant_repo.is_allowed(session, user, tid):
        tid = None

    pair = issue_token_pair(str(user.id), active_tenant=tid)
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        active_tenant_id=tid,
    )
    await session.commit()
    set_auth_cookies(response, pair)
    return LoginResponse(two_factor_required=False, user=await _user_out(session, user, tid))


@router.post("/login", response_model=LoginResponse)
@limiter.limit(_settings.login_rate_limit)
async def login(
    request: Request, response: Response, body: LoginRequest, session: SessionDep
) -> LoginResponse:
    user = await user_repo.get_by_username(session, body.username)
    now = utcnow()

    if user and user.locked_until and user.locked_until > now:
        await audit.record(
            session,
            action=audit.LOGIN_BLOCKED,
            actor=user,
            outcome="failure",
            request=request,
            detail={"reason": "account_locked", "locked_until": user.locked_until.isoformat()},
        )
        await session.commit()
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )

    if user is None or not verify_password(body.password, user.password_hash):
        if user is not None:
            locked = register_failed_attempt(
                user,
                now=now,
                max_failures=_settings.login_max_failures,
                lockout_min=_settings.login_lockout_min,
            )
            if locked:
                log.warning("account_locked", username=user.username, factor="password")
                await audit.record(
                    session,
                    action=audit.ACCOUNT_LOCKED,
                    actor=user,
                    request=request,
                    detail={"factor": "password", "lockout_min": _settings.login_lockout_min},
                )
        # Auch der unbekannte Benutzername wird protokolliert — genau daran erkennt man
        # später ein Durchprobieren von Konten.
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor=user,
            actor_username=body.username if user is None else None,
            outcome="failure",
            request=request,
            detail={"reason": "invalid_credentials"},
        )
        await session.commit()
        raise AuthError("Ungültiger Benutzername oder Passwort.", code="invalid_credentials")

    # Passwort ok
    reset_failed_attempts(user)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)

    # 2FA aktiv -> Zwischenschritt: kurzlebiger 2fa-Cookie, noch keine volle Sitzung.
    if user.totp_enabled and user.totp_secret:
        await session.commit()
        set_2fa_cookie(response, create_2fa_token(str(user.id)))
        return LoginResponse(two_factor_required=True)

    # 2FA-Pflicht, aber noch nicht eingerichtet: ebenfalls keine volle Sitzung. Der
    # 2fa-Cookie erlaubt genau zwei Dinge — einrichten und aktivieren. Erst danach gibt
    # es Tokens. Eine Sitzung auszustellen und die Oberfläche hinterher zu sperren wäre
    # schwächer: Das Access-Token wäre bereits gültig.
    if not user.is_sso and await SettingsService(session).get("auth.require_2fa"):
        await session.commit()
        set_2fa_cookie(response, create_2fa_token(str(user.id)))
        return LoginResponse(two_factor_setup_required=True)

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

    # Auch der zweite Faktor unterliegt der Kontosperre: Wer das Passwort bereits hat,
    # könnte den sechsstelligen Code sonst unbegrenzt raten — das IP-Rate-Limit allein
    # hält einen Angreifer mit mehreren Adressen nicht auf.
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        clear_2fa_cookie(response)
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )

    # Jeder TOTP-Code gilt nur einmal. Er ist rund 90 s gültig; ohne diese Sperre käme
    # jemand, der ihn abfängt (Schulterblick, Mitschnitt), damit ein zweites Mal hinein.
    schritt = matching_step(decrypt(user.totp_secret), body.code)
    ok = schritt is not None and schritt != user.totp_last_step
    if schritt is not None and schritt == user.totp_last_step:
        log.warning("totp_replay_blocked", username=user.username)
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor=user,
            outcome="failure",
            request=request,
            detail={"reason": "totp_replay"},
        )
    if ok:
        user.totp_last_step = schritt
    else:
        # Recovery-Code als Fallback (verbraucht ihn).
        hashes: list[str] = json.loads(user.recovery_codes or "[]")
        matched = match_recovery_code(body.code, hashes)
        if matched:
            hashes.remove(matched)
            user.recovery_codes = json.dumps(hashes)
            ok = True
    if not ok:
        locked = register_failed_attempt(
            user,
            now=now,
            max_failures=_settings.login_max_failures,
            lockout_min=_settings.login_lockout_min,
        )
        await session.commit()
        if locked:
            clear_2fa_cookie(response)
            log.warning("account_locked", username=user.username, factor="2fa")
            await audit.record(
                session,
                action=audit.ACCOUNT_LOCKED,
                actor=user,
                request=request,
                detail={"factor": "2fa", "lockout_min": _settings.login_lockout_min},
            )
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor=user,
            outcome="failure",
            request=request,
            detail={"reason": "invalid_2fa_code"},
        )
        await session.commit()
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")

    reset_failed_attempts(user)
    clear_2fa_cookie(response)
    return await _complete_login(request, response, session, user)


async def _end_if_idle(
    session: SessionDep, response: Response, us: object, now: dt.datetime
) -> bool:
    """Beendet die Sitzung, wenn seit ``last_used_at`` zu lange nichts passiert ist.

    Fängt den geschlossenen Browser und gestohlene Tokens. ``last_used_at`` wird sowohl
    beim Token-Refresh als auch durch den Aktivitäts-Ping des Frontends aktualisiert —
    so bleibt ein aktiv arbeitender Benutzer angemeldet, auch wenn er gerade keine
    API-Aufrufe auslöst. Gibt True zurück, wenn abgemeldet wurde.
    """
    idle_min = _settings.idle_timeout_min
    if idle_min <= 0 or us.last_used_at >= now - dt.timedelta(minutes=idle_min):  # type: ignore[attr-defined]
        return False
    await user_repo.delete_session_by_jti(session, us.refresh_jti)  # type: ignore[attr-defined]
    clear_auth_cookies(response)
    log.info("session_idle_timeout", user_id=us.user_id, idle_min=idle_min)  # type: ignore[attr-defined]
    # Sichtbar machen, dass es eine Abmeldung war — sonst fehlt sie im Protokoll.
    actor = await user_repo.get(session, us.user_id)  # type: ignore[attr-defined]
    await audit.record(session, action=audit.LOGOUT, actor=actor, detail={"reason": "idle_timeout"})
    await session.commit()
    return True


@router.post("/activity", status_code=204)
async def activity(request: Request, response: Response, session: SessionDep) -> Response:
    """Aktivitäts-Ping des Frontends: hält ``last_used_at`` an echter Nutzeraktivität aktuell.

    Ohne diesen Ping würde ``last_used_at`` nur beim Token-Refresh vorrücken. Wer aktiv
    liest oder scrollt, ohne API-Aufrufe auszulösen (z. B. auf einer Seite ohne Polling),
    liefe sonst trotz Aktivität in den Idle-Timeout. Bewusst schlank: keine Token-Rotation,
    kein Body. Ist die Sitzung bereits abgelaufen (z. B. nach Standby), wird sie beendet.
    """
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthError("Nicht angemeldet.", code="not_authenticated")
    try:
        payload = decode_token(token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        raise AuthError("Ungültiges Token.", code="invalid_token") from exc

    us = await user_repo.get_session_by_jti(session, payload["jti"])
    now = utcnow()
    if us is None or us.revoked or us.expires_at < now:
        raise AuthError("Sitzung ungültig.", code="session_invalid")
    if await _end_if_idle(session, response, us, now):
        raise AuthError("Sitzung wegen Inaktivität beendet.", code="session_idle_timeout")

    us.last_used_at = now
    await session.commit()
    return Response(status_code=204)


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

    if await _end_if_idle(session, response, us, now):
        raise AuthError(
            "Sitzung wegen Inaktivität beendet. Bitte erneut anmelden.",
            code="session_idle_timeout",
        )

    user = await user_repo.get(session, us.user_id)
    if user is None or not user.is_active:
        clear_auth_cookies(response)
        raise AuthError("Konto nicht verfügbar.", code="account_unavailable")

    # Rotation in-place: dieselbe Sitzung behält eine Zeile, Token wird ausgetauscht. Der
    # aktive Tenant MUSS erhalten bleiben (`us.active_tenant_id` selbst bleibt unverändert --
    # dieselbe Zeile, kein Reset) -- ohne `active_tenant=` hier verlöre das neue Access-Token
    # den Claim bei JEDEM Refresh (alle `access_token_ttl_min`), und `get_tenant_session`
    # würde den Tenant über `resolve_initial_tenant` neu auflösen statt den zuvor über
    # `/auth/switch-tenant` gewählten aktiven Tenant beizubehalten.
    pair = issue_token_pair(str(user.id), active_tenant=us.active_tenant_id)
    us.refresh_jti = pair.refresh_jti
    us.token_hash = hash_token(pair.refresh_token)
    us.expires_at = pair.refresh_expires
    us.last_used_at = now
    us.user_agent = request.headers.get("user-agent") or us.user_agent
    us.ip_address = (request.client.host if request.client else None) or us.ip_address
    await session.commit()
    set_auth_cookies(response, pair)
    return await _user_out(session, user, us.active_tenant_id)


@router.post("/logout", response_model=Message)
async def logout(request: Request, response: Response, session: SessionDep) -> Message:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, expected_type="refresh")
            # Beim Abmelden die Sitzung entfernen, nicht nur widerrufen: sie soll weder
            # in der Sitzungsliste noch als Datensatz zurückbleiben.
            await user_repo.delete_session_by_jti(session, payload["jti"])
            actor = await user_repo.get(session, int(payload["sub"]))
            await audit.record(session, action=audit.LOGOUT, actor=actor, request=request)
            await session.commit()
        except jwt.PyJWTError:
            pass
    clear_auth_cookies(response)
    return Message(message="Abgemeldet.")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim) -> UserOut:
    return await _user_out(session, user, active_tenant)


@router.post("/switch-tenant", response_model=UserOut)
async def switch_tenant(
    request: Request,
    response: Response,
    body: SwitchTenantRequest,
    user: CurrentUser,
    session: SessionDep,
) -> UserOut:
    """Mandanten-Umschalter (Phase 4a Task 5): setzt den aktiven Mandanten der LAUFENDEN
    Sitzung (nicht bloss den Anzeige-Claim) -- über `is_allowed` gegengeprüft, sonst 403,
    bevor irgendetwas geändert wird. Erst danach: `user_session.active_tenant_id`
    aktualisieren + Token-Paar mit dem neuen Claim neu ausstellen (gleiche Zeile, neue
    `jti`, exakt das Rotationsmuster aus `refresh`/`_complete_login`) + Cookies setzen.
    """
    if not await tenant_repo.is_allowed(session, user, body.tenant_id):
        raise ForbiddenError("Kein Zugriff auf diesen Mandanten.", code="tenant_forbidden")

    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthError("Kein Refresh-Token.", code="no_refresh_token")
    try:
        payload = decode_token(token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        raise AuthError("Ungültiges Refresh-Token.", code="invalid_token") from exc

    us = await user_repo.get_session_by_jti(session, payload["jti"])
    now = utcnow()
    if (
        us is None
        or us.revoked
        or us.user_id != user.id
        or us.token_hash != hash_token(token)
        or us.expires_at < now
    ):
        raise AuthError("Sitzung ungültig. Bitte erneut anmelden.", code="session_invalid")

    # Wie `refresh`: ohne diese Prüfung könnte man den Idle-Timeout umgehen, indem man
    # statt `refresh` einfach `switch-tenant` aufruft -- die Sitzung bliebe dann unbegrenzt
    # am Leben, obwohl niemand mehr aktiv ist.
    if await _end_if_idle(session, response, us, now):
        raise AuthError(
            "Sitzung wegen Inaktivität beendet. Bitte erneut anmelden.",
            code="session_idle_timeout",
        )

    pair = issue_token_pair(str(user.id), active_tenant=body.tenant_id)
    us.refresh_jti = pair.refresh_jti
    us.token_hash = hash_token(pair.refresh_token)
    us.expires_at = pair.refresh_expires
    us.active_tenant_id = body.tenant_id
    us.last_used_at = now
    await audit.record(
        session,
        action=audit.TENANT_SWITCHED,
        actor=user,
        request=request,
        detail={"tenant_id": body.tenant_id},
    )
    await session.commit()
    set_auth_cookies(response, pair)
    return await _user_out(session, user, body.tenant_id)


@router.post("/profile", response_model=UserOut)
async def update_profile(
    body: ProfileUpdate, user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> UserOut:
    user.display_name = (body.display_name or "").strip() or None
    user.updated_at = utcnow()
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


@router.post("/language", response_model=UserOut)
async def set_language(
    body: LanguageUpdate, user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> UserOut:
    user.language = body.language
    user.updated_at = utcnow()
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


@router.get("/me/avatar")
async def get_my_avatar(user: CurrentUser) -> FileResponse:
    path = _avatar_path(user.id) if user.id is not None else None
    if not (path and path.exists()):
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.post("/me/avatar", response_model=UserOut)
async def upload_my_avatar(
    user: CurrentUser,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
    file: UploadFile = File(...),
) -> UserOut:
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
    # Inhalt muss zum behaupteten Typ passen. Pillow würde Fremdes zwar ohnehin ablehnen,
    # aber eine klare Meldung ist besser als ein generisches "Ungültige Bilddatei" —
    # und die Prüfung greift, bevor eine Bibliothek überhaupt an die Bytes geht.
    if not imagetype.matches(data, file.content_type or ""):
        raise PwNotifyError(
            "Der Dateiinhalt passt nicht zum angegebenen Format.", code="content_type_mismatch"
        )
    processed = _process_avatar(data)
    if processed is None:
        raise PwNotifyError("Ungültige Bilddatei.", code="invalid_image")
    _avatar_path(user.id).write_bytes(processed)  # type: ignore[arg-type]
    return await _user_out(session, user, active_tenant)


@router.delete("/me/avatar", response_model=UserOut)
async def delete_my_avatar(
    user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> UserOut:
    if user.is_sso:
        raise PwNotifyError(
            "Das Profilbild wird aus Microsoft Entra übernommen.", code="avatar_sso_managed"
        )
    if user.id is not None:
        _avatar_path(user.id).unlink(missing_ok=True)
    return await _user_out(session, user, active_tenant)


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
    await audit.record(
        session,
        action=audit.SESSIONS_REVOKED,
        actor=user,
        request=request,
        detail={"count": n},
    )
    await session.commit()
    return Message(message=f"{n} andere Sitzung(en) abgemeldet.")


@router.post("/password", response_model=Message)
async def change_password(
    request: Request, body: PasswordChangeRequest, user: CurrentUser, session: SessionDep
) -> Message:
    if not verify_password(body.current_password, user.password_hash):
        raise AuthError("Aktuelles Passwort ist falsch.", code="wrong_current_password")
    user.password_hash = hash_password(body.new_password)
    user.updated_at = utcnow()

    # Ein Passwortwechsel muss fremde Sitzungen beenden. Sonst behält ein gestohlener
    # Refresh-Token vollen Zugriff (bis zu `refresh_token_ttl_days`), obwohl der
    # Benutzer glaubt, ihn mit dem neuen Passwort gerade entzogen zu haben.
    # Die eigene Sitzung bleibt bestehen — sonst meldet der Wechsel einen selbst ab.
    current_jti: str | None = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        with contextlib.suppress(jwt.PyJWTError):
            current_jti = decode_token(token, expected_type="refresh").get("jti")
    revoked = await user_repo.revoke_others(session, user.id, current_jti)  # type: ignore[arg-type]

    await audit.record(
        session,
        action=audit.PASSWORD_CHANGED,
        actor=user,
        request=request,
        detail={"sessions_revoked": revoked},
    )
    await session.commit()
    log.info("password_changed", username=user.username, sessions_revoked=revoked)
    if revoked:
        return Message(message=f"Passwort geändert. {revoked} andere Sitzung(en) abgemeldet.")
    return Message(message="Passwort geändert.")


# --------------------------------------------------------------------------- #
# 2FA (TOTP) — Verwaltung (nur lokale Konten)
# --------------------------------------------------------------------------- #
@router.post("/2fa/setup", response_model=TwoFactorSetupOut)
@limiter.limit(_settings.login_rate_limit)
async def two_factor_setup(
    request: Request, user: EnrollingUser, session: SessionDep
) -> TwoFactorSetupOut:
    if user.is_sso:
        raise PwNotifyError("2FA ist nur für lokale Konten verfügbar.", code="twofa_local_only")
    secret = generate_secret()
    user.totp_secret = encrypt(secret)  # gespeichert, aber noch nicht aktiv
    user.totp_enabled = False
    await session.commit()
    uri = provisioning_uri(secret, user.username)
    return TwoFactorSetupOut(otpauth_uri=uri, qr_png=qr_png_data_uri(uri), secret=secret)


@router.post("/2fa/enable", response_model=RecoveryCodesOut)
@limiter.limit(_settings.login_rate_limit)
async def two_factor_enable(
    request: Request,
    response: Response,
    body: TwoFactorCode,
    user: EnrollingUser,
    session: SessionDep,
) -> RecoveryCodesOut:
    if user.is_sso or not user.totp_secret:
        raise PwNotifyError("2FA-Einrichtung nicht gestartet.", code="twofa_not_started")
    if not verify_totp(decrypt(user.totp_secret), body.code):
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    codes, hashes = generate_recovery_codes()
    user.totp_enabled = True
    user.recovery_codes = json.dumps(hashes)
    user.updated_at = utcnow()
    await audit.record(session, action=audit.TWOFA_ENABLED, actor=user, request=request)

    # Kam der Aufruf über den 2FA-Zwischentoken (erzwungene Einrichtung), fehlt noch die
    # Sitzung — jetzt ist sie verdient. Der Zwischentoken wird ungültig.
    if not request.cookies.get(ACCESS_COOKIE):
        clear_2fa_cookie(response)
        await _complete_login(request, response, session, user)
    else:
        await session.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/2fa/disable", response_model=UserOut)
@limiter.limit(_settings.login_rate_limit)
async def two_factor_disable(
    request: Request,
    body: TwoFactorCode,
    user: CurrentUser,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> UserOut:
    if not user.totp_enabled:
        return await _user_out(session, user, active_tenant)
    valid = (user.totp_secret and verify_totp(decrypt(user.totp_secret), body.code)) or (
        match_recovery_code(body.code, json.loads(user.recovery_codes or "[]")) is not None
    )
    if not valid:
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = None
    user.updated_at = utcnow()
    # Das Abschalten des zweiten Faktors ist ein Angriffsziel — es muss nachvollziehbar sein.
    await audit.record(session, action=audit.TWOFA_DISABLED, actor=user, request=request)
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


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
        # Abgelehnte SSO-Anmeldung protokollieren: Wer vergeblich versucht hereinzukommen,
        # gehört ins Protokoll — sonst sieht man einen Angriff auf die Gruppenzuordnung nie.
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor_username=result.username or None,
            outcome="failure",
            request=request,
            detail={"sso": True, "reason": result.reason or "not_allowed"},
        )
        await session.commit()
        return RedirectResponse(f"{base}/login?sso_denied=1", status_code=302)

    # Tenant-Mapping (Phase 4a Task 4): der `tid`-Claim bestimmt den GENAU EINEN Tenant,
    # an den dieses SSO-Konto gebunden wird -- kein stiller Fallback auf "irgendeinen"
    # Tenant, sonst könnte ein fremder Entra-Tenant Zugriff auf falsche Kundendaten
    # bekommen (die Tenant-Isolation greift erst über `active_tenant`, siehe Task 3).
    tenant = await tenant_repo.get_by_entra_tid(session, result.tid) if result.tid else None
    configured_tid = str(settings.get("graph.tenant_id") or "")
    if tenant is None and result.tid and configured_tid and result.tid == configured_tid:
        # Übergangs-Fallback: die bestehende Single-Tenant-Instanz hat auf ihrem
        # Default-Tenant noch keinen `entra_tenant_id` gesetzt (nullable bis SSO für
        # Multi-Tenant konfiguriert ist). Kommt der Benutzer aus GENAU DEM Entra-Tenant,
        # der für DIESE Instanz konfiguriert ist, bleibt SSO ohne Migrationsschritt
        # funktionsfähig -- und der Default-Tenant wird für künftige Logins direkt an
        # diesen `tid` gebunden (Bootstrap, nur einmal nötig).
        tenant = await tenant_repo.default_tenant(session)
        if tenant.entra_tenant_id is None:
            tenant.entra_tenant_id = result.tid

    if tenant is None:
        # Unbekannter/fremder `tid`: nicht anmelden. Ein Angreifer aus einem fremden
        # Entra-Tenant, der zufällig in einer gleichnamigen Gruppen-ID landet, darf sich
        # so nicht in eine bestehende Instanz einklinken.
        log.warning("oidc_unknown_tenant", tid=result.tid, username=result.username)
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor_username=result.username,
            outcome="failure",
            request=request,
            detail={"sso": True, "reason": "unknown_tenant"},
        )
        await session.commit()
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

    # SSO-Konto ist an genau diesen einen Tenant gebunden (Task 4) -- `tenant_repo`
    # (`allowed_tenant_ids`/`is_allowed`/`resolve_initial_tenant`) liest ausschliesslich
    # `AppUser.tenant_id` für SSO-Konten.
    user.tenant_id = tenant.id

    # Muss hier stehen, weil dieser Pfad bewusst nicht über `_complete_login` läuft
    # (Redirect statt JSON-Antwort). Ohne diesen Eintrag fehlten ausgerechnet die
    # SSO-Anmeldungen im Protokoll — bei aktiviertem SSO also praktisch alle.
    await audit.record(
        session,
        action=audit.LOGIN_SUCCESS,
        actor=user,
        request=request,
        detail={"sso": True, "role": user.role},
    )
    user.last_login_at = utcnow()
    # `active_tenant` schliesst den Task-3-Defer: die SSO-Sitzung trägt den Tenant-Claim
    # direkt ab dem Login, statt ihn bei jedem Request über `resolve_initial_tenant` neu
    # aufzulösen.
    pair = issue_token_pair(str(user.id), active_tenant=tenant.id)
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        active_tenant_id=tenant.id,
    )
    await session.commit()
    # Profilfoto aus Entra holen und cachen (best effort; blockiert Login nicht bei Fehler).
    if user.id is not None:
        with contextlib.suppress(Exception):
            await _cache_sso_avatar(user.id, result.username, settings)
    resp = RedirectResponse(f"{base}/", status_code=302)
    set_auth_cookies(resp, pair)
    return resp
