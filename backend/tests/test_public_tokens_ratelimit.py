"""Neuer Test (Task 7, deferred MINOR 4): echter HTTP-Beweis, dass das Rate-Limit auf den
öffentlichen Token-Endpunkten (`@limiter.limit` in `api/routes/public_tokens.py`) tatsächlich
greift.

ALLE bisherigen Tests dieser Endpunkte (`test_invitation_flow.py`, `test_password_reset_flow.py`)
rufen die Route-Funktion DIREKT auf (mit `request=None` bzw. bewusst deaktiviertem Limiter --
s. `test_invitation_flow.py`s Moduldoku) und beweisen das Rate-Limiting deshalb AUSDRÜCKLICH
NICHT: `slowapi` verlangt für eine echte Auswertung eine ECHTE `starlette.requests.Request`-
Instanz (Client-IP via `get_remote_address`), die ein reiner Python-Funktionsaufruf nicht
liefert.

Treibt daher EINMALIG die volle `create_app()`-ASGI-App über `httpx.AsyncClient` +
`httpx.ASGITransport` an -- OHNE Lifespan (keine Migrationen/kein Scheduler nötig: die
Test-DB ist über die `migrated_engine`-Fixture bereits migriert und `PWNOTIFY_DATABASE_URL`
für die Dauer des Testlaufs auf sie umgebogen, `get_session_factory()` liest das lazy beim
ersten Request). `POST /api/public/token/reset` mit einem garantiert unbekannten Token wird
wiederholt aufgerufen -- jeder einzelne Aufruf scheitert ohnehin generisch mit `403
token_invalid` (keine Enumeration, s. `public_tokens.py`), das ist hier irrelevant: sobald
`login_rate_limit` (Default `10/minute`, dasselbe Limit wie `/auth/login`) überschritten ist,
MUSS `slowapi` mit `429` antworten, BEVOR der Endpunkt überhaupt ausgeführt wird.

`httpx.ASGITransport`s Default-Client ist eine KONSTANTE Adresse (`("127.0.0.1", 123)`) --
alle Aufrufe in dieser Schleife teilen sich also denselben `get_remote_address`-Schlüssel,
genau wie ein echter wiederholender Client hinter derselben IP.

`limiter.reset()` vor UND nach dem Test räumt den In-Memory-Zähler auf: der `Limiter` ist ein
Modul-Singleton (`app.api.deps.limiter`), geteilt mit jeder anderen Suite im selben Lauf --
ohne das Zurücksetzen könnte ein vorheriger/nachfolgender Test denselben Schlüssel bereits
(mit-)ausgeschöpft vorfinden bzw. vorfinden lassen."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from app.api.deps import limiter
from app.core.config import get_settings
from app.main import create_app
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture(autouse=True)
def _rate_limiter_enabled_and_reset() -> Iterator[None]:
    prev = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.enabled = prev


def _configured_limit_count() -> int:
    """`login_rate_limit` hat die Form `"<n>/minute"` (o.ä.) -- nur die Zahl interessiert hier."""
    return int(get_settings().login_rate_limit.split("/", 1)[0])


async def test_public_token_reset_endpoint_returns_429_past_configured_limit(
    migrated_engine: AsyncEngine,
) -> None:
    app = create_app()
    limit = _configured_limit_count()

    transport = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(limit + 3):
            resp = await client.post(
                "/api/public/token/reset",
                json={"token": f"nope-{uuid.uuid4().hex}", "password": "Str0ng!Passw0rd"},
            )
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Rate-Limit hat nie gegriffen -- Status-Folge: {statuses}"
    # Bis zur Grenze generischer 403 (Token existiert nicht) -- danach 429. Kein anderer
    # Status darf auftauchen (z. B. ein 422/500 würde auf ein Setup-Problem hindeuten, nicht
    # auf das hier geprüfte Verhalten).
    assert all(s in (403, 429) for s in statuses)
    # Die ERSTEN `limit` Aufrufe müssen noch durchkommen (403 = generischer Token-Fehlschlag,
    # nicht 429) -- sonst würde dieser Test auch bei einem Limit von "0" trivial grün sein.
    assert statuses[:limit] == [403] * limit
