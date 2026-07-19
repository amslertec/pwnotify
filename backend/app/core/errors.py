"""Domänen-Exceptions und zentrale FastAPI-Exception-Handler."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse

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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PwNotifyError)
    async def _handle(_: Request, exc: PwNotifyError) -> ORJSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, message=exc.message)
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        """Wie FastAPIs Default — aber ohne eingegebene Werte sensibler Felder."""
        return ORJSONResponse(status_code=422, content={"detail": scrub_validation_errors(exc)})


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
