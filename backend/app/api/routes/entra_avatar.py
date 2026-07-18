"""`GET /entra-avatar/{entra_id}`: lazy geholtes, tenant-gescoped gecachtes Graph-Profilfoto.

Gleiche Sichtbarkeit wie `/users` (`CurrentUser`). Die Graph-Config kommt AUSSCHLIESSLICH
aus `TenantSettingsDep` -- der bereits auf den aktiven Mandanten gescopte Settings-Service
(NIE ein ungescoptes `SettingsService.get_all()`, das war der Cross-Tenant-`graph.*`-Bug aus
Commit 2179b2c). Der aktive Mandant (`ActiveTenantClaim`, int) scoped den Cache-Pfad.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from ...services import entra_avatar as avatar_service
from ..deps import ActiveTenantClaim, CurrentUser, TenantSettingsDep

router = APIRouter(prefix="/entra-avatar", tags=["users"])


@router.get("/{entra_id}")
async def get_entra_avatar(
    _: CurrentUser,
    entra_id: str,
    svc: TenantSettingsDep,
    tenant_id: ActiveTenantClaim,
) -> Response:
    settings = await svc.get_all()
    return await avatar_service.serve(entra_id, tenant_id, settings)
