"""Mail-Versand über Microsoft Graph ``sendMail``."""

from __future__ import annotations

from ...core.errors import MailError
from ..graph import GraphClient
from .base import InlineImage


class GraphMailSender:
    backend = "graph"

    def __init__(self, client: GraphClient, sender: str):
        self.client = client
        self.sender = sender

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
        inline_images: list[InlineImage] | None = None,
    ) -> None:
        if not self.sender:
            raise MailError(
                "Keine Absenderadresse konfiguriert (mail.from).", code="mail_no_sender"
            )
        await self.client.send_mail(
            sender=self.sender,
            to=to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            inline_images=inline_images,
        )
