"""Domänen-Exceptions und zentrale FastAPI-Exception-Handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .logging import get_logger

log = get_logger("errors")


class PwNotifyError(Exception):
    """Basisklasse für erwartbare Anwendungsfehler."""

    status_code = 400
    code = "error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class NotFoundError(PwNotifyError):
    status_code = 404
    code = "not_found"


class ConflictError(PwNotifyError):
    status_code = 409
    code = "conflict"


class AuthError(PwNotifyError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(PwNotifyError):
    status_code = 403
    code = "forbidden"


class ValidationError(PwNotifyError):
    status_code = 400
    code = "validation_error"


class SetupRequiredError(PwNotifyError):
    status_code = 409
    code = "setup_required"


class GraphError(PwNotifyError):
    status_code = 502
    code = "graph_error"


class MailError(PwNotifyError):
    status_code = 502
    code = "mail_error"


# Feldnamen, deren Wert niemals in einer Fehlerantwort auftauchen darf. Pydantic legt
# bei Validierungsfehlern den eingegebenen Wert als ``input`` bei — bei einem zu kurzen
# Passwort stünde es damit im Klartext in der Antwort und potenziell in Proxy-Logs.
_SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "current_password",
        "new_password",
        "client_secret",
        "smtp_password",
        "secret",
        "code",
        "token",
    }
)


def _internal_error_response() -> JSONResponse:
    """Generic 500 -- German, without any traceback or internal detail."""
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Interner Serverfehler."}},
    )


class UnhandledExceptionMiddleware(BaseHTTPMiddleware):
    """I2: turn a genuinely unhandled exception into a clean 500 *below* the security-header
    middleware, so the response still flows back through it (CSP/nosniff applied).

    Why a middleware and not just an ``Exception`` exception-handler: in Starlette a handler
    registered for ``Exception``/500 runs in ``ServerErrorMiddleware``, which sits OUTSIDE all
    user middleware -- its response would bypass ``SecurityHeadersMiddleware`` entirely. Caught
    here (inside the header middleware, outside the route's ``ExceptionMiddleware``) the 500
    passes back through the header middleware and gets the security headers. No traceback ever
    reaches the client; the full error is logged server-side.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            log.error("unhandled_exception", path=request.url.path, exc_info=exc)
            return _internal_error_response()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PwNotifyError)
    async def _handle(_: Request, exc: PwNotifyError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Wie FastAPIs Default — aber ohne eingegebene Werte sensibler Felder."""
        return JSONResponse(status_code=422, content={"detail": scrub_validation_errors(exc)})

    # Backstop for an exception raised *outside* UnhandledExceptionMiddleware (e.g. in an
    # outer middleware): still a clean JSON 500 with no traceback, via ServerErrorMiddleware.
    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception_outer", exc_info=exc)
        return _internal_error_response()


def scrub_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Entfernt ``input`` aus Validierungsfehlern sensibler Felder."""
    cleaned: list[dict[str, Any]] = []
    for err in exc.errors():
        item = dict(err)
        if any(str(part) in _SENSITIVE_FIELDS for part in item.get("loc", ())):
            item.pop("input", None)
        # url zeigt nur auf die Pydantic-Doku und bläht die Antwort auf
        item.pop("url", None)
        cleaned.append(item)
    return cleaned
