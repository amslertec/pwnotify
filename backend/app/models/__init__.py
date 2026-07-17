"""SQLModel-Tabellen. Import-Sammelstelle für Alembic-Autogenerate."""

from __future__ import annotations

from .assignment_group import AssignmentGroup, AssignmentGroupTenant
from .audit import AuditLog
from .entra import EntraUser, Exclusion
from .notification import NotificationLog
from .run import Run
from .setting import Setting
from .tenant import AuditorTenant, Tenant
from .token import UserToken
from .user import AppUser, UserSession

__all__ = [
    "AppUser",
    "AssignmentGroup",
    "AssignmentGroupTenant",
    "AuditLog",
    "AuditorTenant",
    "EntraUser",
    "Exclusion",
    "NotificationLog",
    "Run",
    "Setting",
    "Tenant",
    "UserSession",
    "UserToken",
]
