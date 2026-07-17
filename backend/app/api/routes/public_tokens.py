"""Öffentliche, UNAUTHENTIFIZIERTE Endpunkte für Einladungsannahme + Passwort-Reset
(Console+Groups+Invite Task 5, §7b/§7c).

Keine Auth-Dependency irgendwo in diesem Router -- die Sicherheit kommt ausschliesslich aus
dem opaken, gehashten Einmal-Token (`services/user_token.py`, `repositories/
user_token_repo.py`), nicht aus einer Session. Deshalb JEDER Endpunkt zusätzlich
rate-limitiert (dasselbe Limit wie `/auth/login`, `deps.limiter`) -- Brute-Force- UND
Enumerationsschutz auf einem sonst komplett offenen Pfad.

**Keine Enumeration, an keiner Stelle:** ein nie existiertes Token, ein Token mit falschem
`purpose`, ein abgelaufenes und ein bereits verbrauchtes Token liefern IMMER dieselbe
generische Antwort (`TokenInfo(valid=False, ...)` bzw. `ForbiddenError code="token_invalid"`)
-- nie einen Hinweis darauf, ob ein Konto/Token je existiert hat.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.config import get_settings
from ...core.errors import ConflictError, ForbiddenError
from ...core.security import hash_password, hash_token, password_meets_policy
from ...models._base import utcnow
from ...repositories import user_repo, user_token_repo
from ...schemas.auth import TokenAccept, TokenInfo, TokenReset
from ...schemas.common import Message
from ...services import audit
from ..deps import SessionDep, limiter

router = APIRouter(prefix="/public/token", tags=["public-token"])

_settings = get_settings()

_WEAK_PASSWORD_MESSAGE = (
    "Das Passwort erfüllt die Richtlinie nicht (mind. 10 Zeichen, Gross-/Kleinbuchstabe, "
    "Ziffer, Sonderzeichen)."
)


@router.get("/info", response_model=TokenInfo)
@limiter.limit(_settings.login_rate_limit)
async def token_info(request: Request, session: SessionDep, token: str, purpose: str) -> TokenInfo:
    """Lässt die Accept-/Reset-Seite einen Token VOR dem Absenden validieren + die
    Zieladresse anzeigen -- ohne je zu verraten, WARUM ein Token ungültig ist."""
    if purpose not in ("invite", "reset"):
        return TokenInfo(valid=False)
    row = await user_token_repo.get_live_by_hash(session, hash_token(token), purpose)
    if row is None:
        return TokenInfo(valid=False)
    target = await user_repo.get(session, row.app_user_id)
    if target is None:
        return TokenInfo(valid=False)
    return TokenInfo(valid=True, email=target.email, purpose=purpose)


@router.post("/accept", response_model=Message)
@limiter.limit(_settings.login_rate_limit)
async def accept_token(request: Request, body: TokenAccept, session: SessionDep) -> Message:
    """Löst eine Einladung ein: aus dem `pending:<uuid4>`-Platzhalterkonto (`admin_users.
    create_local`, Einladungsmodus) wird ein echtes, einlogg-bares lokales Konto.

    Reihenfolge bewusst: Token verifizieren -> Passwort-Policy (serverseitig, die
    Frontend-Checkliste ist nur UX) -> Benutzername-Eindeutigkeit HIER geprüft (nicht beim
    Einladen, da der Name zu diesem Zeitpunkt noch gar nicht feststand). Ein
    `username_taken`-Fehlschlag verbraucht das Token NICHT -- sonst könnte man nach einem
    Kollisions-Versuch nicht mehr mit einem anderen Namen erneut einlösen."""
    row = await user_token_repo.get_live_by_hash(session, hash_token(body.token), "invite")
    if row is None:
        raise ForbiddenError("Einladung ungültig oder abgelaufen.", code="token_invalid")
    if not password_meets_policy(body.password):
        raise ForbiddenError(_WEAK_PASSWORD_MESSAGE, code="password_policy")

    target = await user_repo.get(session, row.app_user_id)
    if target is None:
        raise ForbiddenError("Einladung ungültig oder abgelaufen.", code="token_invalid")

    existing = await user_repo.get_by_username(session, body.username)
    if existing is not None:
        raise ConflictError("Benutzername bereits vergeben.", code="username_taken")

    target.username = body.username
    target.display_name = f"{body.first_name} {body.last_name}".strip()
    target.password_hash = hash_password(body.password)
    target.is_active = True
    target.updated_at = utcnow()
    await user_token_repo.consume(session, row)

    await audit.record(
        session,
        action=audit.INVITATION_ACCEPTED,
        actor=target,
        target=target.username,
        request=request,
    )
    await session.commit()
    return Message(message="Konto aktiviert. Sie können sich jetzt anmelden.")


@router.post("/reset", response_model=Message)
@limiter.limit(_settings.login_rate_limit)
async def reset_token(request: Request, body: TokenReset, session: SessionDep) -> Message:
    """Setzt das Passwort über einen zuvor vom Admin ausgelösten Reset-Link (`admin_users.
    send_reset`). Bewusst KEIN Benutzername im Body -- das Konto ist über das Token bereits
    fixiert. Reaktiviert ein deaktiviertes Konto NICHT (nur das Passwort ändert sich)."""
    row = await user_token_repo.get_live_by_hash(session, hash_token(body.token), "reset")
    if row is None:
        raise ForbiddenError("Link ungültig oder abgelaufen.", code="token_invalid")
    if not password_meets_policy(body.password):
        raise ForbiddenError(_WEAK_PASSWORD_MESSAGE, code="password_policy")

    target = await user_repo.get(session, row.app_user_id)
    if target is None:
        raise ForbiddenError("Link ungültig oder abgelaufen.", code="token_invalid")

    target.password_hash = hash_password(body.password)
    target.updated_at = utcnow()
    await user_token_repo.consume(session, row)

    await audit.record(
        session,
        action=audit.PASSWORD_RESET_DONE,
        actor=target,
        target=target.username,
        request=request,
    )
    await session.commit()
    return Message(message="Passwort geändert. Sie können sich jetzt anmelden.")
