"""Mail-Versand über SMTP (stdlib smtplib, im Thread ausgeführt)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from ...core.errors import MailError
from .base import InlineImage


class SmtpMailSender:
    backend = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        tls_mode: str,
        sender: str,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls_mode = tls_mode  # starttls | ssl | none
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
        if not self.host:
            raise MailError("Kein SMTP-Host konfiguriert.", code="smtp_no_host")
        if not self.sender:
            raise MailError(
                "Keine Absenderadresse konfiguriert (mail.from).", code="mail_no_sender"
            )
        try:
            await asyncio.to_thread(
                self._send_sync, to, subject, html_body, text_body, inline_images or []
            )
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(str(exc), code="smtp_send_failed") from exc

    def _send_sync(
        self,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None,
        inline_images: list[InlineImage],
    ) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(to)
        msg.set_content(text_body or "Bitte HTML-fähigen Client verwenden.")
        msg.add_alternative(html_body, subtype="html")

        # Inline-Bilder als related an den HTML-Teil hängen (cid:<content_id>).
        if inline_images:
            html_part = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
            if isinstance(html_part, EmailMessage):
                for cid, content, mime in inline_images:
                    maintype, _, subtype = mime.partition("/")
                    html_part.add_related(
                        content, maintype=maintype, subtype=subtype, cid=f"<{cid}>"
                    )

        context = ssl.create_default_context()
        if self.tls_mode == "ssl":
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as server:
                self._auth_and_send(server, msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                if self.tls_mode == "starttls":
                    server.starttls(context=context)
                self._auth_and_send(server, msg)

    def _auth_and_send(self, server: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.username and self.password:
            server.login(self.username, self.password)
        server.send_message(msg)
