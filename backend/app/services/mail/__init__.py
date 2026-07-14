"""Mail-Versand: gemeinsames Interface, Graph- und SMTP-Backend, Factory."""

from __future__ import annotations

from typing import Any

from ..graph import GraphClient, GraphConfig
from .base import MailSender
from .graph_sender import GraphMailSender
from .smtp_sender import SmtpMailSender

__all__ = ["GraphMailSender", "MailSender", "SmtpMailSender", "build_sender"]


def build_sender(settings: dict[str, Any]) -> MailSender:
    """Erzeugt den konfigurierten Mail-Sender aus den effektiven Settings."""
    backend = settings.get("mail.backend", "graph")
    sender_addr = settings.get("mail.from") or ""
    if backend == "smtp":
        return SmtpMailSender(
            host=settings.get("mail.smtp_host") or "",
            port=int(settings.get("mail.smtp_port") or 587),
            username=settings.get("mail.smtp_username") or None,
            password=settings.get("mail.smtp_password") or None,
            tls_mode=settings.get("mail.smtp_tls") or "starttls",
            sender=sender_addr,
        )
    graph = GraphClient(
        GraphConfig(
            tenant_id=settings.get("graph.tenant_id") or "",
            client_id=settings.get("graph.client_id") or "",
            client_secret=settings.get("graph.client_secret") or "",
            cloud=settings.get("graph.cloud") or "global",
        )
    )
    return GraphMailSender(graph, sender=sender_addr)
