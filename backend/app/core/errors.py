"""Domänen-Exceptions und zentrale FastAPI-Exception-Handler."""

from __future__ import annotations

from fastapi import FastAPI, Request
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


class SetupRequiredError(PwNotifyError):
    status_code = 409
    code = "setup_required"


class GraphError(PwNotifyError):
    status_code = 502
    code = "graph_error"


class MailError(PwNotifyError):
    status_code = 502
    code = "mail_error"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PwNotifyError)
    async def _handle(_: Request, exc: PwNotifyError) -> ORJSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, message=exc.message)
        return ORJSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
