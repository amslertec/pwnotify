"""Gemeinsames Mail-Sender-Interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Eingebettetes Inline-Bild: (content_id, bytes, mime-type).
InlineImage = tuple[str, bytes, str]


@runtime_checkable
class MailSender(Protocol):
    backend: str

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
        inline_images: list[InlineImage] | None = None,
    ) -> None:
        """Versendet eine Mail an ``to``. Wirft MailError bei Fehlschlag."""
        ...
