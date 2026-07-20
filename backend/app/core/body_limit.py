"""ASGI request-body size guard (M5).

The per-route upload checks (`branding.py`, `auth.py`) only run *after* `await file.read()`
has already spooled the whole body to disk -- so a multi-GB body is written out before any
2/5 MB limit fires. This middleware rejects over-large requests at the transport layer,
before any handler touches the body:

* a present ``Content-Length`` is trusted and rejected immediately (not a single byte read);
* streamed bytes are counted as a fallback for chunked bodies without a ``Content-Length``.

On violation it returns a clean ``413 Payload Too Large`` with the app's JSON error envelope.
It is mounted inside ``SecurityHeadersMiddleware`` so the 413 still carries the security headers.
"""

from __future__ import annotations

import orjson
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class MaxBodySizeMiddleware:
    """Reject requests whose body exceeds ``max_bytes`` before any handler reads them."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(send)
            return

        state = {"total": 0, "rejected": False}

        async def guarded_receive() -> Message:
            if state["rejected"]:
                # The body is already over the limit -- tell the app the client is gone so it
                # stops trying to read; our 413 below is the authoritative response.
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["total"] += len(message.get("body", b""))
                if state["total"] > self.max_bytes:
                    state["rejected"] = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            # Once we have decided to reject, drop whatever (late) response the app produces --
            # we emit our own 413 after it returns.
            if not state["rejected"]:
                await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception:
            # A stream reader may raise (e.g. ClientDisconnect) once we inject the disconnect;
            # that is expected when we are rejecting. Re-raise anything unrelated.
            if not state["rejected"]:
                raise

        if state["rejected"]:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        limit_mb = self.max_bytes // (1024 * 1024)
        body = orjson.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"Die Anfrage ist zu gross (max. {limit_mb} MB).",
                }
            }
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
