"""Der Verbindungstest muss `GroupMember.Read.All` verlangen, sobald Gruppen im Spiel sind.

Ohne diese Berechtigung scheitern gruppenbasierter Sync und SSO-Rollen-Mapping mit 403 —
der Test meldete aber „alle Berechtigungen vorhanden“ und schickte einen beim Einrichten
auf die falsche Fährte.
"""

from __future__ import annotations

import pytest
from app.services.connectivity import required_group_permissions
from app.services.graph.client import GROUP_PERMISSION, REQUIRED_PERMISSIONS


def test_no_group_configured_does_not_require_permission() -> None:
    """Wer keine Gruppe nutzt, soll nicht zu einer unnötigen Berechtigung gedrängt werden."""
    assert required_group_permissions({}) == []
    assert required_group_permissions({"sync.group_id": "", "oidc.admin_group_id": None}) == []


@pytest.mark.parametrize("key", ["sync.group_id", "oidc.admin_group_id", "oidc.auditor_group_id"])
def test_each_group_setting_requires_permission(key: str) -> None:
    assert required_group_permissions({key: "00000000-0000-0000-0000-000000000001"}) == [
        GROUP_PERMISSION
    ]


def test_permission_is_not_demanded_unconditionally() -> None:
    """Sie gehört bewusst NICHT zu den Basisrechten — sonst falscher Alarm ohne Gruppen."""
    assert GROUP_PERMISSION not in REQUIRED_PERMISSIONS
