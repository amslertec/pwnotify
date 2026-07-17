"""TDD für Task 4 des Context-Gating-v2-Inkrements: Backend-Routen-Gating gegen Matrix B
(SECURITY-CRITICAL, Backend-Defense-in-Depth hinter dem Frontend-Gating aus Task 5).

Schaltet der Superadmin in einen Kunden-Kontext um, sieht er dessen operative Sicht wie ein
Kunden-Admin -- Instanz-Einstellungen (Mode-Schalter + Default-Umbenennung), die Mandanten-
Konsole (CRUD) und die Zuweisungs-Konsole sind Provider-Ebene-Aktionen, die NUR aus dem
Default-Kontext heraus erlaubt sind (Design §4/§4-notes). `require_superadmin_default_context`
(`deps.py`) setzt das durch: `Depends(require_superadmin)` läuft zuerst (Rollen-Gate), danach
wird der aktive Mandant über `_resolve_authorized_tenant` aufgelöst und gegen
`default_tenant_id` geprüft.

Treibt die Route-Funktionen direkt an (wie `test_admin_instance.py`/`test_admin_tenants.py`/
`test_admin_assignments.py`) -- keine dieser Routen öffnet selbst eine zusätzliche Session,
die gewöhnliche savepoint-isolierte `session`-Fixture genügt, kein manuelles Aufräumen nötig.
Da FastAPIs Dependency-Injection hier NICHT läuft (reine Python-Aufrufe), wird die Ketten-
Komposition manuell nachgebildet: `guarded = await require_superadmin_default_context(request,
user, session)` (bzw. `require_superadmin`/`require_admin` für die anderen Gates), erst danach
die Route selbst -- exakt das Muster, das die bestehenden Tests für `SuperadminUser`/`AdminUser`
bereits verwenden.

Ein echtes signiertes Access-Token (`issue_token_pair`) im `_FakeRequest`-Cookie treibt den
`active_tenant`-Claim, den `_resolve_authorized_tenant` liest -- das Token-Subjekt ist dabei
irrelevant (`_claimed_active_tenant` liest nur den Claim, lädt keinen Benutzer nach)."""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import (
    ACCESS_COOKIE,
    require_admin,
    require_superadmin,
    require_superadmin_default_context,
)
from app.api.routes.admin_assignments import get_assignments, set_assignments
from app.api.routes.admin_instance import update_instance
from app.api.routes.admin_tenants import create_tenant, delete_tenant, update_tenant
from app.api.routes.admin_users import create_superadmin, set_superadmin
from app.api.routes.auth import me
from app.core.errors import ForbiddenError
from app.core.security import issue_token_pair
from app.models.user import AppUser
from app.repositories import tenant_repo, user_repo
from app.schemas.assignment import AssignmentUpdate
from app.schemas.auth import SuperadminCreate, SuperadminToggle
from app.schemas.instance import InstanceUpdate
from app.schemas.tenant import TenantCreate, TenantUpdate
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRequest:
    """Duck-typed Request -- Guard/Route lesen nur `.cookies` (Audit-Aufrufe in den
    Erfolgsfällen nutzen zusätzlich `.headers`/`.client`, hier bewusst leer/None wie in
    `test_active_tenant_resolution.py`s `_FakeLoginRequest`)."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.client: object | None = None


def _slug() -> str:
    return f"mb-{uuid.uuid4().hex[:10]}"


async def _mk_superadmin(session: AsyncSession) -> AppUser:
    user = AppUser(
        username=f"mb-superadmin-{uuid.uuid4().hex[:8]}", password_hash="x", role="superadmin"
    )
    session.add(user)
    await session.flush()
    return user


async def _mk_admin(session: AsyncSession) -> AppUser:
    """Lokaler (NICHT-Super-)Admin -- besteht `require_superadmin` NICHT (Design §6)."""
    user = AppUser(username=f"mb-admin-{uuid.uuid4().hex[:8]}", password_hash="x", role="admin")
    session.add(user)
    await session.flush()
    return user


async def _mk_auditor(session: AsyncSession) -> AppUser:
    user = AppUser(username=f"mb-auditor-{uuid.uuid4().hex[:8]}", password_hash="x", role="auditor")
    session.add(user)
    await session.flush()
    return user


def _request_with_claim(user_id: int, tenant_id: int | None) -> _FakeRequest:
    pair = issue_token_pair(str(user_id), active_tenant=tenant_id)
    return _FakeRequest({ACCESS_COOKIE: pair.access_token})


# ---- Default-Kontext: Schreibzugriffe gelingen (auch die Single-Tenant-Regression) -------- #


async def test_superadmin_default_context_allows_instance_tenant_and_assignment_writes(
    session: AsyncSession,
) -> None:
    """Superadmin mit `active_tenant`-Claim == Default-Tenant: PUT /admin/instance, die
    Mandanten-CRUD-Konsole und GET/PUT /admin/assignments gelingen allesamt. Das ist zugleich
    die Single-Tenant-Regression: mit dem Mode-Flag AUS (Default) ist der aktive Kontext des
    Superadmins IMMER der Default-Tenant -- exakt dieses Szenario."""
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    default = await tenant_repo.default_tenant(session)
    request = _request_with_claim(superadmin.id, default.id)

    guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    assert guarded is superadmin

    # Instanz-Schreibzugriff -- nur die Umbenennung (nicht `multi_tenant_mode`, das über eine
    # EIGENE, echt committende `tenant_scoped_session` läuft, siehe `test_admin_instance.py`s
    # Cross-Connection-Hinweis; hier nicht nötig, kein manuelles Aufräumen).
    inst_out = await update_instance(
        request,  # type: ignore[arg-type]
        guarded,
        InstanceUpdate(default_tenant_name="Matrix B Default"),
        session,
    )
    assert inst_out.default_tenant_name == "Matrix B Default"

    # Mandanten-Konsole: Create -> Update -> Delete.
    created = await create_tenant(
        request,  # type: ignore[arg-type]
        guarded,
        TenantCreate(name="Matrix B Co", slug=_slug()),
        session,
    )
    assert created.id is not None
    updated = await update_tenant(
        request,  # type: ignore[arg-type]
        guarded,
        created.id,
        TenantUpdate(name="Matrix B Co Renamed"),
        session,
    )
    assert updated.name == "Matrix B Co Renamed"
    deleted = await delete_tenant(request, guarded, created.id, session)  # type: ignore[arg-type]
    assert deleted.message

    # Zuweisungs-Konsole (leeres Reconcile gegen einen frischen lokalen Admin -- No-Op, aber
    # beweist, dass der Routenkörper tatsächlich läuft statt am Guard zu scheitern).
    target = await _mk_admin(session)
    assert target.id is not None
    got = await get_assignments(guarded, target.id, session)  # type: ignore[arg-type]
    assert got.tenant_ids == []
    put = await set_assignments(
        request,  # type: ignore[arg-type]
        guarded,
        target.id,
        AssignmentUpdate(tenant_ids=[]),
        session,
    )
    assert put.tenant_ids == []

    # Superadmin-Verwaltung (Whole-Branch-Review Context-Gating v2, Finding 2): seit dem Fix
    # `SuperadminDefaultContextUser`-gegatet wie die anderen Provider-Ebene-Konsolen oben --
    # `create_superadmin` und `set_superadmin` gelingen im Default-Kontext.
    created_superadmin = await create_superadmin(
        request,  # type: ignore[arg-type]
        guarded,
        SuperadminCreate(username=f"mb-new-superadmin-{uuid.uuid4().hex[:8]}", password="x" * 12),
        session,
    )
    assert created_superadmin.role == "superadmin"

    promote_target = await _mk_admin(session)
    assert promote_target.id is not None
    promoted = await set_superadmin(
        request,  # type: ignore[arg-type]
        guarded,
        promote_target.id,
        SuperadminToggle(promote=True),
        session,
    )
    assert promoted.role == "superadmin"


async def test_superadmin_claimless_token_resolves_to_default_context(
    session: AsyncSession,
) -> None:
    """Kein `active_tenant`-Claim (z. B. ein frisch ausgestelltes Token vor dem ersten
    Umschalten) -- `_resolve_authorized_tenant` fällt auf `resolve_initial_tenant` zurück,
    das für einen Superadmin GENAU den Default-Tenant liefert. Der Guard muss durchlassen,
    nicht 403 auslösen -- der Default-Kontext ist der natürliche Ausgangszustand."""
    superadmin = await _mk_superadmin(session)
    request = _FakeRequest()  # kein Cookie überhaupt
    guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
    assert guarded is superadmin


# ---- Kunden-Kontext: Schreibzugriffe werden verweigert (nicht-vakuos) --------------------- #


async def test_superadmin_customer_context_blocks_instance_write(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer A", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await update_instance(
            request,  # type: ignore[arg-type]
            guarded,
            InstanceUpdate(default_tenant_name="Should Not Apply"),
            session,
        )
    assert exc_info.value.code == "default_context_required"

    # Kein Teilschreiben -- der Name blieb unverändert.
    default = await tenant_repo.default_tenant(session)
    assert default.name != "Should Not Apply"


async def test_superadmin_customer_context_blocks_tenant_console_create(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer B", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await create_tenant(
            request,  # type: ignore[arg-type]
            guarded,
            TenantCreate(name="Should Not Exist", slug=_slug()),
            session,
        )
    assert exc_info.value.code == "default_context_required"


async def test_superadmin_customer_context_blocks_tenant_console_update(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer C", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await update_tenant(
            request,  # type: ignore[arg-type]
            guarded,
            customer.id,
            TenantUpdate(name="Should Not Rename"),
            session,
        )
    assert exc_info.value.code == "default_context_required"
    refreshed = await tenant_repo.get(session, customer.id)
    assert refreshed is not None
    assert refreshed.name == "Matrix B Customer C"


async def test_superadmin_customer_context_blocks_tenant_console_delete(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer D", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await delete_tenant(request, guarded, customer.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "default_context_required"
    assert await tenant_repo.get(session, customer.id) is not None


async def test_superadmin_customer_context_blocks_assignment_console(
    session: AsyncSession,
) -> None:
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer E", slug=_slug())
    assert customer.id is not None
    target = await _mk_admin(session)
    assert target.id is not None
    request = _request_with_claim(superadmin.id, customer.id)

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await get_assignments(guarded, target.id, session)  # type: ignore[arg-type]
    assert exc_info.value.code == "default_context_required"

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await set_assignments(
            request,  # type: ignore[arg-type]
            guarded,
            target.id,
            AssignmentUpdate(tenant_ids=[]),
            session,
        )
    assert exc_info.value.code == "default_context_required"


async def test_superadmin_customer_context_blocks_superadmin_crud(session: AsyncSession) -> None:
    """Whole-Branch-Review Context-Gating v2, Finding 2: `create_superadmin`/
    `set_superadmin` were only `SuperadminUser`-gated (reachable from ANY context) --
    inconsistent with the invariant that superadmin management is Provider-Ebene, like the
    other consoles above. Fixed to `SuperadminDefaultContextUser`; this proves the block from
    a customer context and that neither route made ANY change (no superadmin created, target
    role unchanged)."""
    superadmin = await _mk_superadmin(session)
    assert superadmin.id is not None
    customer = await tenant_repo.create(session, name="Matrix B Customer F", slug=_slug())
    assert customer.id is not None
    request = _request_with_claim(superadmin.id, customer.id)
    new_username = f"mb-blocked-superadmin-{uuid.uuid4().hex[:8]}"

    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await create_superadmin(
            request,  # type: ignore[arg-type]
            guarded,
            SuperadminCreate(username=new_username, password="x" * 12),
            session,
        )
    assert exc_info.value.code == "default_context_required"
    assert await user_repo.get_by_username(session, new_username) is None

    promote_target = await _mk_admin(session)
    assert promote_target.id is not None
    with pytest.raises(ForbiddenError) as exc_info:
        guarded = await require_superadmin_default_context(request, superadmin, session)  # type: ignore[arg-type]
        await set_superadmin(
            request,  # type: ignore[arg-type]
            guarded,
            promote_target.id,
            SuperadminToggle(promote=True),
            session,
        )
    assert exc_info.value.code == "default_context_required"
    refreshed_target = await user_repo.get(session, promote_target.id)
    assert refreshed_target is not None
    assert refreshed_target.role == "admin"


# ---- Guard-Reihenfolge: Nicht-Superadmin scheitert am Rollen-Gate, nicht am Kontext-Gate -- #


async def test_local_admin_is_rejected_by_superadmin_gate_first(session: AsyncSession) -> None:
    """`SuperadminDefaultContextUser` komponiert `Depends(require_superadmin)` ALS
    Sub-Dependency von `SuperadminUser` -- FastAPI löst diese zuerst auf. Ein lokaler
    (Nicht-Super-)Admin muss deshalb `superadmin_required` sehen, nicht
    `default_context_required` (obwohl er, hätte er den Kontext-Check überhaupt erreicht,
    auch daran gescheitert wäre, da er keinem Tenant zugeordnet ist)."""
    admin = await _mk_admin(session)
    with pytest.raises(ForbiddenError) as exc_info:
        await require_superadmin(admin)  # type: ignore[arg-type]
    assert exc_info.value.code == "superadmin_required"


# ---- Matrix B, bereits bestehende Gates (Assert-only, keine Code-Änderung) ---------------- #


async def test_auditor_is_rejected_from_audit_routes(session: AsyncSession) -> None:
    """`audit.list_audit`/`audit.list_actions` sind `AdminUser`-gegatet
    (`Depends(require_admin)`) -- ein Auditor (`role=='auditor'`) besteht `require_admin`
    nicht."""
    auditor = await _mk_auditor(session)
    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(auditor)  # type: ignore[arg-type]
    assert exc_info.value.code == "admin_required"


async def test_auditor_is_rejected_from_settings_write_routes(session: AsyncSession) -> None:
    """`settings.update`/`graph_test`/`mail_test`/`template_reset`/Exclusions-Add&Delete sind
    allesamt `AdminUser`-gegatet -- derselbe Guard wie oben, hier stellvertretend für alle
    Settings-SCHREIB-Routen (die Settings-LESE-Routen bleiben bewusst `CurrentUser`, siehe
    `deps.py`-Docstrings/Task-4-Brief: Tenant-scoped über RLS, kein Cross-Tenant-Leck, das
    Ausblenden der Settings-Seite für Auditoren ist eine Frontend-Angelegenheit, Task 5)."""
    auditor = await _mk_auditor(session)
    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(auditor)  # type: ignore[arg-type]
    assert exc_info.value.code == "admin_required"


async def test_auditor_is_rejected_from_access_user_management_write_routes(
    session: AsyncSession,
) -> None:
    """`admin_users.create_local`/`set_role`/`delete_user` sind `AdminUser`-gegatet --
    derselbe Guard wie oben, hier stellvertretend für die Benutzerverwaltungs-SCHREIB-Routen
    der `/access`-Seite."""
    auditor = await _mk_auditor(session)
    with pytest.raises(ForbiddenError) as exc_info:
        await require_admin(auditor)  # type: ignore[arg-type]
    assert exc_info.value.code == "admin_required"


# ---- /auth/me: `active_tenant_is_default` ------------------------------------------------- #


async def test_auth_me_reports_active_tenant_is_default(session: AsyncSession) -> None:
    superadmin = await _mk_superadmin(session)
    default = await tenant_repo.default_tenant(session)
    customer = await tenant_repo.create(session, name="Matrix B Me Customer", slug=_slug())
    assert customer.id is not None

    out_default = await me(superadmin, session, default.id)  # type: ignore[arg-type]
    assert out_default.active_tenant_is_default is True

    out_customer = await me(superadmin, session, customer.id)  # type: ignore[arg-type]
    assert out_customer.active_tenant_is_default is False

    # Randfall: kein aktiver Mandant zugeordnet -- niemals stillschweigend "Default" behaupten.
    out_none = await me(superadmin, session, None)  # type: ignore[arg-type]
    assert out_none.active_tenant_is_default is False
