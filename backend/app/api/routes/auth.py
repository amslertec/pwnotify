"""Local authentication (JWT cookies, refresh rotation, brute-force protection)."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import secrets
import uuid
from pathlib import Path

import jwt
from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from ...core import imagetype
from ...core.config import get_settings
from ...core.crypto import decrypt, encrypt
from ...core.errors import AuthError, ForbiddenError, NotFoundError, PwNotifyError
from ...core.http import client_ip, client_user_agent
from ...core.logging import get_logger
from ...core.security import (
    WEAK_PASSWORD_MESSAGE,
    create_2fa_token,
    decode_token,
    hash_password,
    hash_token,
    issue_token_pair,
    needs_rehash,
    password_meets_policy,
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
)
from ...db.tenant_context import tenant_scoped_session
from ...models._base import utcnow
from ...models.user import AppUser
from ...repositories import assignment_group_repo, tenant_repo, user_repo
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
    TwoFactorDisable,
    TwoFactorSetupOut,
    UserOut,
)
from ...schemas.common import Message
from ...services import audit, instance_settings, oidc
from ...services.graph import GraphClient, GraphConfig
from ...services.settings_service import SettingsService, effective_base_url
from ..deps import (
    ACCESS_COOKIE,
    OIDC_FLOW_COOKIE,
    REFRESH_COOKIE,
    TWOFA_COOKIE,
    ActiveTenantClaim,
    CurrentUser,
    EnrollingUser,
    SessionDep,
    clear_2fa_cookie,
    clear_auth_cookies,
    clear_oidc_flow_cookie,
    default_tenant_id,
    limiter,
    set_2fa_cookie,
    set_auth_cookies,
    set_oidc_flow_cookie,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()
log = get_logger("auth")

_AVATAR_ALLOWED = {"image/png", "image/jpeg", "image/webp"}
_AVATAR_MAX_BYTES = 5 * 1024 * 1024

# A valid Argon2id hash of a random, never-used secret. `login` verifies against this when
# no user matches the submitted username, so the Argon2 cost is paid on every attempt --
# without it, an unknown username short-circuits the password check and answers measurably
# faster than a known one, letting an attacker enumerate accounts (M7).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


# --------------------------------------------------------------------------- #
# Avatar (locally uploaded or cached from Entra) -- as a square PNG
# --------------------------------------------------------------------------- #
def _avatar_dir() -> Path:
    # Deliberately NO mkdir here: this function is also used on pure READ paths
    # (`_user_out`, GET /me/avatar). An mkdir on a non-writable/unreachable
    # `data_dir` (fresh deploy before volume mount, tests/CI without `/data`) would otherwise
    # raise `PermissionError: '/data'` and 500 every user serialization. The directory
    # is created ONLY immediately before a write access (see `_ensure_avatar_dir`).
    return Path(_settings.data_dir) / "avatars"


def _ensure_avatar_dir() -> Path:
    d = _avatar_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _avatar_path(user_id: int) -> Path:
    return _avatar_dir() / f"{user_id}.png"


def _avatar_mtime(user_id: int) -> int | None:
    """Avatar mtime as a cache buster, or `None` if no image exists or the
    `data_dir` is not readable. Every `OSError` (missing file, unreachable `/data`)
    -> "no avatar", so user serialization never 500s on a filesystem state."""
    try:
        return int(_avatar_path(user_id).stat().st_mtime)
    except OSError:
        return None


# M9: without a cap, Pillow decodes whatever pixel area a file declares -- a tiny file
# claiming a huge width/height forces a huge in-memory bitmap allocation (decompression
# bomb). 24 MP comfortably covers any legitimate avatar photo while still catching bombs;
# Pillow raises `Image.DecompressionBombError` (a plain `Exception`) once decoded pixels
# exceed 2x this value, which the `except Exception` below already turns into a clean `None`.
_MAX_IMAGE_PIXELS = 24_000_000


def _process_avatar(data: bytes) -> bytes | None:
    """Crop image centered to a square -> 256x256 PNG. None on error."""
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS

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
    except Exception:  # invalid/corrupt image file
        return None


async def _cache_sso_avatar(user_id: int, upn: str, settings: dict[str, object]) -> None:
    """Fetch the SSO user's profile photo from Entra and cache it as an avatar (best effort)."""
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
        _ensure_avatar_dir()
        _avatar_path(user_id).write_bytes(processed)


async def _user_out(session: SessionDep, user: AppUser, active_tenant_id: int | None) -> UserOut:
    """UserOut incl. avatar status (existence + mtime as cache buster) and tenant info
    (Phase 4a Task 5): the active tenant (from claim/session, unverified -- display only,
    see the `ActiveTenantClaim` docstring) plus the list of tenants this account is
    allowed to switch to (`tenant_repo.allowed_tenant_ids`: None -> all active ones, otherwise
    exactly these, further restricted to actually ACTIVE tenants). The default
    tenant is ALWAYS at position 0 of `switchable_tenants` (rest sorted by name, Design
    §8) -- so every consumer sees the same, deterministic order. `UserOut.multi_tenant_mode`
    (Task 5) additionally carries the instance-wide switch state, read via
    `services.instance_settings.read_mode` (always default-tenant-scoped, see there).
    `UserOut.active_tenant_is_default` (Context-Gating v2, Matrix B) reports whether the active
    tenant is the default tenant -- reuses `default` below (already resolved for the
    switchable-first sort), no extra query."""
    avatar_mtime = _avatar_mtime(user.id) if user.id is not None else None

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

    default = await tenant_repo.default_tenant(session)
    switchable.sort(key=lambda t: t.id != default.id)

    return UserOut(
        id=user.id,  # type: ignore[arg-type]
        username=user.username,
        display_name=user.display_name,
        is_sso=user.is_sso,
        role=user.role,
        language=user.language,
        two_factor_enabled=user.totp_enabled,
        last_login_at=user.last_login_at,
        has_avatar=avatar_mtime is not None,
        avatar_version=avatar_mtime or 0,
        idle_timeout_min=_settings.idle_timeout_min,
        email=user.email,
        active_tenant=active_tenant,
        switchable_tenants=switchable,
        multi_tenant_mode=await instance_settings.read_mode(session),
        active_tenant_is_default=active_tenant_id is not None and active_tenant_id == default.id,
    )


async def _complete_login(
    request: Request, response: Response, session: SessionDep, user: AppUser
) -> LoginResponse:
    """Issue full tokens + create a session (after password or 2FA code)."""
    # Determine the active tenant for login -- ALWAYS re-check against `is_allowed`, don't
    # blindly take `resolve_initial_tenant`: its resolution does NOT gate on
    # is_active (see its docstring), otherwise e.g. a meanwhile deactivated,
    # own SSO tenant would end up as the active claim in the token/`active_tenant_id` -- an
    # availability gap, not a cross-tenant leak, but still wrong. Uniform for all
    # account types, no special case.
    #
    # Resolved BEFORE the audit record below (Task 7/M11) so `LOGIN_SUCCESS` can be
    # tenant-attributed with the SAME `tid` the session actually logs into -- moved up from
    # its original position after the audit call, no other change to this resolution.
    tid = await tenant_repo.resolve_initial_tenant(session, user)
    if tid is not None and not await tenant_repo.is_allowed(session, user, tid):
        tid = None

    await audit.record(
        session,
        action=audit.LOGIN_SUCCESS,
        actor=user,
        request=request,
        detail={"sso": user.is_sso, "two_factor": user.totp_enabled},
        # Owner-session route (Task 7/M11): attribute to the tenant this login actually
        # resolved into -- NULL stays NULL when no tenant is resolvable (`tid is None`).
        tenant_id=tid,
    )
    user.last_login_at = utcnow()
    await user_repo.prune_sessions(session, user.id)  # type: ignore[arg-type]

    pair = issue_token_pair(str(user.id), active_tenant=tid, generation=user.token_generation)
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=client_user_agent(request),
        ip=client_ip(request),
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

    # Always run an Argon2 verification, win or lose -- against the real hash for a known
    # user, against `_DUMMY_HASH` for an unknown one. This costs the same either way, so an
    # unknown username can no longer be told apart from a known one by response time (M7).
    password_ok = verify_password(
        body.password, user.password_hash if user is not None else _DUMMY_HASH
    )

    if user is None or not password_ok:
        if user is not None and not (user.locked_until and user.locked_until > now):
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
        # The unknown username is logged too -- that's exactly how account
        # enumeration attempts are spotted later on.
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

    # Password correct -- ONLY now may the lockout be disclosed, since only someone who
    # already knows the password can learn anything from it (no longer an enumeration vector).
    if user.locked_until and user.locked_until > now:
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

    reset_failed_attempts(user)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)

    # 2FA active -> intermediate step: short-lived 2fa cookie, no full session yet.
    if user.totp_enabled and user.totp_secret:
        await session.commit()
        set_2fa_cookie(response, create_2fa_token(str(user.id)))
        return LoginResponse(two_factor_required=True)

    # 2FA required but not yet set up: also no full session. The
    # 2fa cookie allows exactly two things -- setting up and activating. Only after that
    # are tokens issued. Issuing a session and locking down the UI afterwards would be
    # weaker: the access token would already be valid.
    if not user.is_sso:
        # `auth.require_2fa` is a PER-TENANT setting (no `instance.` prefix in
        # `settings_schema.py`): each customer governs its own requirement. Reading it on the
        # owner session (no tenant context) would fall back to the DEFAULT tenant and apply the
        # wrong customer's value -- a security control governed by the wrong tenant. Read it
        # through an explicitly tenant-scoped session bound to the account's HOME tenant instead
        # of setting the tenant `ContextVar` around the owner session (F-03): the former would
        # make the begin-listener arm `SET LOCAL ROLE pwnotify_app` on the owner connection if a
        # new transaction opened mid-read, and the later owner-session writes in `_complete_login`
        # (user_session INSERT, audit) have no rights under `pwnotify_app` -> login breaks. The
        # scoped read runs on its own runtime connection (RLS-enforced) and leaves this `session`
        # untouched. `user.tenant_id` may be None for an instance-wide local admin; resolve the
        # default tenant then -- its correct home.
        home_tid = user.tenant_id
        if home_tid is None:
            home_tid = await default_tenant_id(session)
        async with tenant_scoped_session(home_tid) as tscoped:
            require_2fa = await SettingsService(tscoped).get("auth.require_2fa")
        if require_2fa:
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

    # The second factor is subject to the account lockout too: whoever already has the
    # password could otherwise guess the six-digit code without limit -- the IP rate limit alone
    # does not stop an attacker with multiple addresses.
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        clear_2fa_cookie(response)
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )

    # Every TOTP code is valid only once. It is valid for roughly 90 s; without this lock,
    # someone who intercepts it (shoulder surfing, capture) could get in a second time with it.
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
        # Recovery code as fallback (consumes it).
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
    """Ends the session if nothing has happened since ``last_used_at`` for too long.

    Catches the closed browser and stolen tokens. ``last_used_at`` is updated both
    on token refresh and by the frontend's activity ping -- so an actively
    working user stays logged in even while not currently triggering
    API calls. Returns True if the session was ended.
    """
    idle_min = _settings.idle_timeout_min
    if idle_min <= 0 or us.last_used_at >= now - dt.timedelta(minutes=idle_min):  # type: ignore[attr-defined]
        return False
    await user_repo.delete_session_by_jti(session, us.refresh_jti)  # type: ignore[attr-defined]
    # Bump the generation so the already-issued access token dies WITH the session. Deleting
    # the refresh row alone does not help: `get_current_user` never reads the session row, it
    # gates on the `gen` claim vs `AppUser.token_generation`. Without this, a stolen access
    # token would outlive the idle logout by up to `access_token_ttl_min` (analogous to the
    # other revocation paths: logout/revoke_all/change_password all bump here too).
    await user_repo.bump_token_generation(session, us.user_id)  # type: ignore[attr-defined]
    clear_auth_cookies(response)
    log.info("session_idle_timeout", user_id=us.user_id, idle_min=idle_min)  # type: ignore[attr-defined]
    # Make it visible that this was a logout -- otherwise it's missing from the audit trail.
    actor = await user_repo.get(session, us.user_id)  # type: ignore[attr-defined]
    await audit.record(session, action=audit.LOGOUT, actor=actor, detail={"reason": "idle_timeout"})
    await session.commit()
    return True


@router.post("/activity", status_code=204)
@limiter.limit(_settings.auth_refresh_rate_limit)
async def activity(request: Request, response: Response, session: SessionDep) -> Response:
    """Frontend activity ping: keeps ``last_used_at`` current with real user activity.

    Without this ping, ``last_used_at`` would only advance on token refresh. Someone actively
    reading or scrolling without triggering API calls (e.g. on a page without polling)
    would otherwise hit the idle timeout despite being active. Deliberately lean: no token
    rotation, no body. If the session has already expired (e.g. after standby), it is ended.
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
@limiter.limit(_settings.auth_refresh_rate_limit)
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
    # Reuse/theft detection: known but revoked/mismatched token -> lock everything down.
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

    # In-place rotation: the same session keeps one row, the token gets swapped out. The
    # active tenant MUST be preserved (`us.active_tenant_id` itself stays unchanged --
    # same row, no reset) -- without `active_tenant=` here, the new access token would lose
    # the claim on EVERY refresh (every `access_token_ttl_min`), and `get_tenant_session`
    # would re-resolve the tenant via `resolve_initial_tenant` instead of keeping the active
    # tenant previously chosen via `/auth/switch-tenant`.
    pair = issue_token_pair(
        str(user.id), active_tenant=us.active_tenant_id, generation=user.token_generation
    )
    us.refresh_jti = pair.refresh_jti
    us.token_hash = hash_token(pair.refresh_token)
    us.expires_at = pair.refresh_expires
    us.last_used_at = now
    us.user_agent = client_user_agent(request) or us.user_agent
    us.ip_address = client_ip(request) or us.ip_address
    await session.commit()
    set_auth_cookies(response, pair)
    return await _user_out(session, user, us.active_tenant_id)


@router.post("/logout", response_model=Message)
async def logout(request: Request, response: Response, session: SessionDep) -> Message:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, expected_type="refresh")
            # On logout, remove the session rather than just revoking it: it should remain
            # neither in the session list nor as a record.
            await user_repo.delete_session_by_jti(session, payload["jti"])
            actor = await user_repo.get(session, int(payload["sub"]))
            if actor is not None and actor.id is not None:
                # Kills the caller's current access token immediately (its `gen` claim is
                # now stale) instead of leaving it valid for up to `access_token_ttl_min`
                # after logout -- the session row is already gone above.
                await user_repo.bump_token_generation(session, actor.id)
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
    """Tenant switcher (Phase 4a Task 5): sets the active tenant of the CURRENT
    session (not just the display claim) -- re-checked against `is_allowed`, otherwise 403,
    before anything is changed. Only then: update `user_session.active_tenant_id`
    + reissue the token pair with the new claim (same row, new
    `jti`, exactly the rotation pattern from `refresh`/`_complete_login`) + set cookies.
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

    # Like `refresh`: without this check, the idle timeout could be bypassed by simply
    # calling `switch-tenant` instead of `refresh` -- the session would then stay alive
    # indefinitely even though no one is active anymore.
    if await _end_if_idle(session, response, us, now):
        raise AuthError(
            "Sitzung wegen Inaktivität beendet. Bitte erneut anmelden.",
            code="session_idle_timeout",
        )

    pair = issue_token_pair(
        str(user.id), active_tenant=body.tenant_id, generation=user.token_generation
    )
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
    request: Request,
    body: ProfileUpdate,
    user: CurrentUser,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> UserOut:
    """Task 5, §7d: `email` is editable ONLY for local accounts -- an SSO account gets
    its address from Entra, a value entered here would only mask it without changing
    the actual Entra account. The field is therefore silently IGNORED for an SSO
    account (no error -- the frontend hides it there anyway).
    This address is also the anchor the admin reset trigger (§7c) sends to."""
    user.display_name = (body.display_name or "").strip() or None
    if not user.is_sso:
        user.email = (body.email or "").strip() or None
    user.updated_at = utcnow()
    # Audit (finding L3): a self-service profile change. The new display name/email are the
    # user's own PII, so they stay OUT of `detail` -- the actor identity is enough for the trail.
    await audit.record(session, action=audit.PROFILE_UPDATED, actor=user, request=request)
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


@router.post("/language", response_model=UserOut)
async def set_language(
    request: Request,
    body: LanguageUpdate,
    user: CurrentUser,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> UserOut:
    user.language = body.language
    user.updated_at = utcnow()
    # Audit (finding L3): the language code is not PII, so it is safe to record.
    await audit.record(
        session,
        action=audit.LANGUAGE_CHANGED,
        actor=user,
        request=request,
        detail={"language": body.language},
    )
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


@router.get("/me/avatar")
async def get_my_avatar(user: CurrentUser) -> FileResponse:
    if user.id is None or _avatar_mtime(user.id) is None:
        raise NotFoundError("Kein Profilbild vorhanden.", code="no_avatar")
    return FileResponse(
        _avatar_path(user.id), media_type="image/png", headers={"Cache-Control": "no-cache"}
    )


@router.post("/me/avatar", response_model=UserOut)
async def upload_my_avatar(
    request: Request,
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
    # Content must match the claimed type. Pillow would reject something bogus anyway,
    # but a clear message is better than a generic "invalid image file" --
    # and the check kicks in before any library even touches the bytes.
    if not imagetype.matches(data, file.content_type or ""):
        raise PwNotifyError(
            "Der Dateiinhalt passt nicht zum angegebenen Format.", code="content_type_mismatch"
        )
    processed = _process_avatar(data)
    if processed is None:
        raise PwNotifyError("Ungültige Bilddatei.", code="invalid_image")
    _ensure_avatar_dir()
    _avatar_path(user.id).write_bytes(processed)  # type: ignore[arg-type]
    # Audit (finding L3): self-service avatar change -- only the operation, never the image.
    await audit.record(
        session,
        action=audit.AVATAR_CHANGED,
        actor=user,
        request=request,
        detail={"op": "upload"},
    )
    await session.commit()
    return await _user_out(session, user, active_tenant)


@router.delete("/me/avatar", response_model=UserOut)
async def delete_my_avatar(
    request: Request, user: CurrentUser, session: SessionDep, active_tenant: ActiveTenantClaim
) -> UserOut:
    if user.is_sso:
        raise PwNotifyError(
            "Das Profilbild wird aus Microsoft Entra übernommen.", code="avatar_sso_managed"
        )
    if user.id is not None:
        _avatar_path(user.id).unlink(missing_ok=True)
    await audit.record(
        session,
        action=audit.AVATAR_CHANGED,
        actor=user,
        request=request,
        detail={"op": "delete"},
    )
    await session.commit()
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
    request: Request, user: CurrentUser, session: SessionDep, response: Response
) -> Message:
    current_jti = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        with contextlib.suppress(jwt.PyJWTError):
            current_jti = decode_token(token, expected_type="refresh").get("jti")
    n = await user_repo.revoke_others(session, user.id, current_jti)  # type: ignore[arg-type]

    # Like change_password: the revoked sessions must not keep still-valid access tokens
    # for up to 15 minutes -- a lost/stolen device must be cut off
    # IMMEDIATELY. Bumping the generation invalidates ALL of the user's access tokens
    # (including their own, currently held one); the caller's own session is therefore
    # immediately reissued afterwards with the new generation so the caller
    # doesn't get locked out themselves.
    await user_repo.bump_token_generation(session, user.id)  # type: ignore[arg-type]
    await session.refresh(user)
    if current_jti is not None:
        us = await user_repo.get_session_by_jti(session, current_jti)
        if us is not None:
            pair = issue_token_pair(
                str(user.id), active_tenant=us.active_tenant_id, generation=user.token_generation
            )
            us.refresh_jti = pair.refresh_jti
            us.token_hash = hash_token(pair.refresh_token)
            us.expires_at = pair.refresh_expires
            us.last_used_at = utcnow()
            set_auth_cookies(response, pair)

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
@limiter.limit(_settings.login_rate_limit)
async def change_password(
    request: Request,
    body: PasswordChangeRequest,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> Message:
    # The current-password reauth is a brute-force surface just like login and 2FA-disable
    # (F-02): a hijacked session could otherwise guess `current_password` -- bounded only by
    # the per-IP rate limit -- and then take the account over via a new password. Honour the
    # lockout here, and count+audit+commit a wrong password so the account actually locks.
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )
    if not verify_password(body.current_password, user.password_hash):
        locked = register_failed_attempt(
            user,
            now=now,
            max_failures=_settings.login_max_failures,
            lockout_min=_settings.login_lockout_min,
        )
        if locked:
            log.warning("account_locked", username=user.username, factor="change_password")
            await audit.record(
                session,
                action=audit.ACCOUNT_LOCKED,
                actor=user,
                request=request,
                detail={"factor": "change_password", "lockout_min": _settings.login_lockout_min},
            )
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor=user,
            outcome="failure",
            request=request,
            detail={"reason": "wrong_current_password", "context": "change_password"},
        )
        await session.commit()
        raise AuthError("Aktuelles Passwort ist falsch.", code="wrong_current_password")
    # Full server-side password policy (Security Phase 5, Task 2) -- pydantic's
    # `min_length=10` on `PasswordChangeRequest.new_password` is only a floor.
    if not password_meets_policy(body.new_password):
        raise ForbiddenError(WEAK_PASSWORD_MESSAGE, code="password_policy")
    # Successful reauth clears any partial failure counter (login-path parity, F-02).
    reset_failed_attempts(user)
    user.password_hash = hash_password(body.new_password)
    user.updated_at = utcnow()

    # A password change must end other sessions. Otherwise a stolen
    # refresh token keeps full access (for up to `refresh_token_ttl_days`), even though the
    # user believes they just revoked it with the new password.
    # The caller's own session stays alive -- otherwise the change would log them out too.
    current_jti: str | None = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        with contextlib.suppress(jwt.PyJWTError):
            current_jti = decode_token(token, expected_type="refresh").get("jti")
    revoked = await user_repo.revoke_others(session, user.id, current_jti)  # type: ignore[arg-type]

    # Task 2 (L1): invalidate ALL of the user's access tokens -- including the caller's own,
    # currently held one -- then immediately re-issue a fresh pair for THIS device/session so
    # the caller stays logged in with a gen-N token instead of being locked out by their own
    # password change. `session.refresh` picks up the value the UPDATE just wrote.
    await user_repo.bump_token_generation(session, user.id)  # type: ignore[arg-type]
    await session.refresh(user)
    if current_jti is not None:
        us = await user_repo.get_session_by_jti(session, current_jti)
        if us is not None:
            pair = issue_token_pair(
                str(user.id), active_tenant=us.active_tenant_id, generation=user.token_generation
            )
            us.refresh_jti = pair.refresh_jti
            us.token_hash = hash_token(pair.refresh_token)
            us.expires_at = pair.refresh_expires
            us.last_used_at = utcnow()
            set_auth_cookies(response, pair)

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
# 2FA (TOTP) -- management (local accounts only)
# --------------------------------------------------------------------------- #
@router.post("/2fa/setup", response_model=TwoFactorSetupOut)
@limiter.limit(_settings.login_rate_limit)
async def two_factor_setup(
    request: Request, user: EnrollingUser, session: SessionDep
) -> TwoFactorSetupOut:
    if user.is_sso:
        raise PwNotifyError("2FA ist nur für lokale Konten verfügbar.", code="twofa_local_only")
    if user.totp_enabled:
        # Re-enrollment while 2FA is active is forbidden: an interim 2FA token (issued to a
        # 2FA-enabled account after password-only login) must not be usable to overwrite the
        # stored secret. Re-enrollment is only possible after an authenticated `disable`.
        raise ForbiddenError(
            "2FA ist bereits aktiv. Zum Neueinrichten zuerst deaktivieren.",
            code="twofa_already_enabled",
        )
    secret = generate_secret()
    user.totp_secret = encrypt(secret)  # stored, but not yet active
    user.totp_enabled = False
    # Security Phase 5, Task 8/M10: issuing a fresh TOTP secret/QR is itself security-
    # relevant (an account takeover of the enrollment step could plant an attacker's own
    # secret) -- no `detail`, the secret itself must never reach the audit log.
    await audit.record(session, action=audit.TWOFA_SETUP_STARTED, actor=user, request=request)
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
    if user.totp_enabled:
        raise ForbiddenError(
            "2FA ist bereits aktiv. Zum Neueinrichten zuerst deaktivieren.",
            code="twofa_already_enabled",
        )
    # Consume the enrollment code the same way `two_factor_verify` consumes a login code:
    # record the matched step in `totp_last_step` so the exact code just typed cannot be
    # replayed at `/2fa/verify` for the rest of its ~30-90 s TOTP validity window.
    step = matching_step(decrypt(user.totp_secret), body.code)
    if step is None:
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    codes, hashes = generate_recovery_codes()
    user.totp_enabled = True
    user.totp_last_step = step
    user.recovery_codes = json.dumps(hashes)
    user.updated_at = utcnow()
    await audit.record(session, action=audit.TWOFA_ENABLED, actor=user, request=request)

    # If the call came in via the 2FA interim token (forced setup), the session is
    # still missing -- it's now earned. The interim token becomes invalid.
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
    body: TwoFactorDisable,
    user: CurrentUser,
    session: SessionDep,
    active_tenant: ActiveTenantClaim,
) -> UserOut:
    if not user.totp_enabled:
        return await _user_out(session, user, active_tenant)

    # The password re-auth below is a brute-force surface just like login (a hijacked session
    # could otherwise guess the plaintext password, bounded only by the per-IP rate limit).
    # Honour the account lockout HERE too (F-02): without this check the lockout set below would
    # never bite on this path, since the session stays valid regardless of `locked_until`.
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        raise AuthError(
            "Konto vorübergehend gesperrt. Bitte später erneut versuchen.", code="account_locked"
        )

    # Re-authenticate with the current password: disabling the second factor is a high-value
    # action, and a hijacked SESSION alone must not suffice (L1). Password first, then code --
    # both must be correct. A wrong password counts as a failed attempt and can lock the account
    # (F-02), mirroring the login handler; the commit is mandatory, or the counter/lock is rolled
    # back on the raise (same trap as F-01/H1).
    if not verify_password(body.password, user.password_hash):
        locked = register_failed_attempt(
            user,
            now=now,
            max_failures=_settings.login_max_failures,
            lockout_min=_settings.login_lockout_min,
        )
        if locked:
            log.warning("account_locked", username=user.username, factor="2fa_disable")
            await audit.record(
                session,
                action=audit.ACCOUNT_LOCKED,
                actor=user,
                request=request,
                detail={"factor": "2fa_disable", "lockout_min": _settings.login_lockout_min},
            )
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor=user,
            outcome="failure",
            request=request,
            detail={"reason": "invalid_password", "context": "2fa_disable"},
        )
        await session.commit()
        raise AuthError("Passwort falsch.", code="invalid_password")

    # Replay-safe TOTP check, exactly like /2fa/verify and /2fa/enable: match the step and
    # advance totp_last_step so the same code cannot be used to log in AND then disable within
    # its ~90 s window (L1). A recovery code stays a valid fallback.
    step = matching_step(decrypt(user.totp_secret), body.code) if user.totp_secret else None
    totp_ok = step is not None and step != user.totp_last_step
    if totp_ok:
        user.totp_last_step = step
    else:
        recovery_hashes = json.loads(user.recovery_codes or "[]")
        totp_ok = match_recovery_code(body.code, recovery_hashes) is not None
    if not totp_ok:
        raise AuthError("Ungültiger 2FA-Code.", code="invalid_2fa_code")
    # Successful re-auth clears any partial failure counter, exactly like the login path's
    # `reset_failed_attempts` -- otherwise stale failures would linger and lock the account early.
    reset_failed_attempts(user)
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = None
    user.updated_at = utcnow()
    # Disabling the second factor is an attack target -- it must be traceable.
    await audit.record(session, action=audit.TWOFA_DISABLED, actor=user, request=request)
    await session.commit()
    await session.refresh(user)
    return await _user_out(session, user, active_tenant)


# --------------------------------------------------------------------------- #
# SSO / OIDC (Microsoft Entra)
# --------------------------------------------------------------------------- #
@router.get("/config")
async def auth_config(session: SessionDep) -> dict[str, object]:
    """Public: tells the login page whether SSO is available (+ button text)."""
    settings = await SettingsService(session).get_all()
    return {
        "oidc_enabled": oidc.is_configured(settings),
        "oidc_button_label": settings.get("oidc.button_label") or "Mit Microsoft anmelden",
    }


def _redirect_uri(base: str) -> str:
    return f"{base}/api/auth/oidc/callback"


@router.get("/oidc/login")
@limiter.limit(_settings.login_rate_limit)
async def oidc_login(
    request: Request, session: SessionDep, login_hint: str | None = None
) -> RedirectResponse:
    # Rate-limited (audit I4, RFC 9700): this unauthenticated route is an outbound amplifier --
    # every hit makes the server perform an MSAL token-exchange setup and (on callback) a Graph
    # lookup. `request` is required for slowapi's per-client key function.
    settings = await SettingsService(session).get_all()
    base = effective_base_url(settings)
    url, flow_cookie = oidc.initiate_login(settings, _redirect_uri(base), login_hint=login_hint)
    resp = RedirectResponse(url, status_code=302)
    set_oidc_flow_cookie(resp, flow_cookie)
    return resp


# POST, not GET: with response_mode=form_post (RFC 9700 §4.3.1) Entra delivers the callback as a
# cross-site POST whose form body carries code/state/error -- read them from the form, not the
# query string. This does NOT open a new CSRF surface: the exchange is still gated on the
# encrypted, browser-bound flow cookie (Fernet, state-bound) plus the `state` parameter, neither
# of which an attacker can forge; PKCE/nonce/state remain enforced by MSAL unchanged.
# Rate-limited (audit I4): same unauthenticated outbound-amplifier reasoning as `oidc_login`.
@router.post("/oidc/callback")
@limiter.limit(_settings.login_rate_limit)
async def oidc_callback(
    request: Request,
    session: SessionDep,
    code: str | None = Form(default=None),
    state: str | None = Form(default=None),
    error: str | None = Form(default=None),
) -> RedirectResponse:
    settings = await SettingsService(session).get_all()
    base = effective_base_url(settings)
    if error or not code or not state:
        return RedirectResponse(f"{base}/login?sso_error=1", status_code=302)

    flow_cookie = request.cookies.get(OIDC_FLOW_COOKIE)
    if not flow_cookie:
        # No browser-bound state cookie -> reject (login-CSRF / stale/replayed callback).
        return RedirectResponse(f"{base}/login?sso_error=1", status_code=302)
    try:
        flow = oidc.decode_flow_cookie(flow_cookie)
        result = await oidc.exchange_and_verify(settings, flow, {"code": code, "state": state})
    except AuthError, PwNotifyError:
        return RedirectResponse(f"{base}/login?sso_error=1", status_code=302)
    # Security fix (Phase 4c Task 4): `result.allowed`/`result.role` are computed against the
    # OWNER session -- `SettingsService(session).get_all()` above reads an undefined mix of
    # the `oidc.*` rows of ALL customers without RLS filtering, once a
    # second SSO customer exists. These values must therefore NOT decide admission
    # HERE -- the authoritative check happens further below, AFTER the `tid` resolution,
    # against the settings OF the customer actually found. Only what is not resolvable
    # regardless of customer is rejected here: no username in the token, or
    # no group information determinable (`result.groups is None` -- neither token claim
    # nor Graph lookup) -- without groups, no role can be determined for ANY customer.
    if not result.username or result.groups is None:
        # Log the rejected SSO login attempt: anyone who fails to get in
        # belongs in the audit trail -- otherwise an attack on group mapping would never be seen.
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

    # Tenant mapping (Phase 4a Task 4): the `tid` claim determines the ONE EXACT tenant
    # this SSO account gets bound to -- no silent fallback to "some"
    # tenant, otherwise a foreign Entra tenant could gain access to the wrong customer's data
    # (tenant isolation only kicks in via `active_tenant`, see Task 3).
    tenant = await tenant_repo.get_by_entra_tid(session, result.tid) if result.tid else None
    configured_tid = str(settings.get("graph.tenant_id") or "")
    if tenant is None and result.tid and configured_tid and result.tid == configured_tid:
        # Transitional fallback: the existing single-tenant instance has not yet set an
        # `entra_tenant_id` on its default tenant (nullable until SSO is
        # configured for multi-tenant). If the user comes from EXACTLY the Entra tenant
        # configured for THIS instance, SSO keeps working without a migration
        # step -- and the default tenant is bound directly to this `tid` for future
        # logins (bootstrap, only needed once).
        tenant = await tenant_repo.default_tenant(session)
        if tenant.entra_tenant_id is None:
            tenant.entra_tenant_id = result.tid

    if tenant is None:
        # Unknown/foreign `tid`: do not log in. An attacker from a foreign
        # Entra tenant who happens to land in a same-named group ID must not
        # be able to piggyback into an existing instance this way.
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

    # Determine role + admission AUTHORITATIVELY (security fix, Phase 4c Task 4; provider-only
    # SSO, Tenant Refinements Task 4) -- NOT `result.role`/`result.allowed`, which were computed
    # above against the owner-session settings mix. Two disjoint paths, separated solely by
    # the instance mode: in MULTI-TENANT mode, SSO is exclusively a provider
    # staff login -- EVERY SSO login (even with a customer `tid`) authorizes via TEAM
    # membership (`AssignmentGroup`) and is homed on the default tenant (see
    # `home_tenant_id` below). Only SINGLE-TENANT mode stays byte-exact with the per-customer
    # role-group settings.
    assert tenant.id is not None  # persisted row from the DB
    multi_tenant = await instance_settings.read_mode(session)
    if multi_tenant:
        # Provider staff (multi-tenant mode, regardless of the matched `tid`): admission +
        # role come from TEAM membership (`AssignmentGroup`), NOT from any tenant's
        # role-group settings -- fail-closed with no settings fallback.
        role, allowed = await oidc.resolve_group_role(session, result.groups)
        reason = None if allowed else "not_in_any_team"
    else:
        # UNCHANGED (single-tenant mode): per-customer settings role groups.
        # `tenant_scoped_session` reads via the app role + RLS GUC, so it is GUARANTEED to see
        # only this one tenant's `oidc.*` rows. In the single-tenant/transitional case (bootstrap
        # above), customer and owner settings are identical -- same result as before, no
        # behavior change.
        async with tenant_scoped_session(tenant.id) as tsession:
            tenant_settings = await SettingsService(tsession).get_all()
        role, allowed, reason = oidc.resolve_role(result.groups, tenant_settings)
    if not allowed:
        # The user is from a known Entra tenant, but in NONE of THIS customer's
        # eligible groups -- e.g. a member of customer A's admin group,
        # but not of customer B's groups. This is exactly the closed gap: without this
        # re-resolution, `result.role`/`result.allowed` (instance mix) would incorrectly
        # have granted or denied access.
        log.warning(
            "oidc_role_denied_for_tenant",
            tid=result.tid,
            tenant_id=tenant.id,
            username=result.username,
        )
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor_username=result.username,
            outcome="failure",
            request=request,
            detail={"sso": True, "reason": reason or "not_in_tenant_group"},
        )
        await session.commit()
        return RedirectResponse(f"{base}/login?sso_denied=1", status_code=302)

    # Determine home tenant ONCE (provider-only SSO, Task 4): in multi-tenant mode, EVERY
    # SSO account is homed on the default tenant (the provider) -- regardless of the matched
    # `tid`. In single-tenant mode, the matched tenant is the default anyway (bootstrap above),
    # so this is a no-op. Home and the session's initial `active_tenant` MUST match:
    # `resolve_initial_tenant` returns `AppUser.tenant_id` for SSO accounts; a customer
    # `active_tenant` with a default home would boot the account into a customer context it may
    # hold no grant for.
    home_tenant_id = (await tenant_repo.default_tenant(session)).id if multi_tenant else tenant.id

    user = await user_repo.get_by_username(session, result.username)
    if user is None:
        # SSO user with no local password (unusable random hash).
        user = await user_repo.create(
            session,
            username=result.username,
            password_hash=hash_password(uuid.uuid4().hex),
            display_name=result.display_name,
            role=role,
            is_sso=True,
        )
    elif not user.is_sso:
        # A local (non-SSO) account already owns this username. Never flip it to SSO: doing so
        # would let an Entra identity take over a local admin/superadmin and lock the real owner
        # out (`require_superadmin` needs `not is_sso`). Fail-safe: deny the login, change nothing.
        log.warning("oidc_local_account_conflict", username=result.username, role=user.role)
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor_username=result.username,
            outcome="failure",
            request=request,
            detail={"sso": True, "reason": "local_account_exists"},
        )
        await session.commit()
        return RedirectResponse(f"{base}/login?sso_denied=1", status_code=302)
    elif not user.is_active:
        # Deny a disabled SSO account before mutating it / creating a session + LOGIN_SUCCESS.
        # Its tokens would be inert anyway (get_current_user/refresh gate on `is_active`), but
        # proceeding would leave an orphan session row and a misleading LOGIN_SUCCESS in the
        # audit trail. Mirror the other SSO deny-paths (local_account_exists / not_in_group):
        # no reactivation, just fail-closed.
        log.warning("oidc_inactive_account", username=result.username)
        await audit.record(
            session,
            action=audit.LOGIN_FAILED,
            actor_username=result.username,
            outcome="failure",
            request=request,
            detail={"sso": True, "reason": "inactive"},
        )
        await session.commit()
        return RedirectResponse(f"{base}/login?sso_denied=1", status_code=302)
    else:
        user.is_sso = True
        user.display_name = result.display_name
        user.role = role  # role follows the Entra group membership OF the matched customer

    # SSO account is bound to exactly its home tenant (Task 4) -- `tenant_repo`
    # (`allowed_tenant_ids`/`is_allowed`/`resolve_initial_tenant`) reads exclusively
    # `AppUser.tenant_id` for SSO accounts.
    user.tenant_id = home_tenant_id

    # Group reconcile (SECURITY-CRITICAL, Console+Groups+Invite Task 4): materializes
    # `source='group'` grants from this login's team memberships (`result.groups`).
    # Runs on EVERY SSO login; the `is_provider_account` gate in `reconcile_group_grants`
    # (first line, fail-closed) turns it into a no-op for a CUSTOMER-homed account -- its
    # home tenant is its customer, not the default tenant. No group grant for customer
    # accounts, no touching manual grants. Deliberately NOT in the password login path
    # (local accounts are never in Entra groups).
    await assignment_group_repo.reconcile_group_grants(session, user, result.groups)

    # Must be here because this path deliberately does not go through `_complete_login`
    # (redirect instead of JSON response). Without this entry, of all things SSO logins
    # would be missing from the audit trail -- with SSO enabled, that's practically all of them.
    await audit.record(
        session,
        action=audit.LOGIN_SUCCESS,
        actor=user,
        request=request,
        detail={"sso": True, "role": user.role},
        # Owner-session route (Task 7/M11): `home_tenant_id` is fixed above -- always a
        # real tenant here (never NULL), unlike the SSO-DENY paths above, which stay NULL.
        tenant_id=home_tenant_id,
    )
    user.last_login_at = utcnow()
    # `active_tenant` closes the Task 3 defer: the SSO session carries the tenant claim
    # right from login, instead of re-resolving it via `resolve_initial_tenant` on
    # every request.
    pair = issue_token_pair(
        str(user.id), active_tenant=home_tenant_id, generation=user.token_generation
    )
    await user_repo.create_session(
        session,
        user_id=user.id,  # type: ignore[arg-type]
        jti=pair.refresh_jti,
        token_hash=hash_token(pair.refresh_token),
        expires_at=pair.refresh_expires,
        user_agent=client_user_agent(request),
        ip=client_ip(request),
        active_tenant_id=home_tenant_id,
    )
    await session.commit()
    # Fetch profile photo from Entra and cache it (best effort; does not block login on failure).
    if user.id is not None:
        with contextlib.suppress(Exception):
            await _cache_sso_avatar(user.id, result.username, settings)
    resp = RedirectResponse(f"{base}/", status_code=302)
    set_auth_cookies(resp, pair)
    clear_oidc_flow_cookie(resp)
    return resp
