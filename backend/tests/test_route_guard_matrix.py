"""Introspective route-guard soll-matrix test (S2, Phase 3 Task 5).

Existing gating tests (`test_matrix_b_route_gating.py`, `test_route_tenant_scoping.py`) check
only hand-picked routes. This test iterates the ENTIRE actual router and compares every
`(method, path)` against an explicit expected matrix (`EXPECTED`), so a newly added UNGUARDED
route (a future C3/H1/H2-class mistake) is flagged automatically instead of silently shipping.

**Deviation from the plan sketch (Task 5 brief, "How guards are inspectable"):** the plan
assumed `for r in app.routes: isinstance(r, APIRoute)` finds every route directly. In the
FastAPI version actually pinned here, `app.include_router(...)` no longer eagerly flattens
child routers into `app.routes` -- each inclusion is wrapped in an internal `_IncludedRouter`
lazy-resolution node, and `app.routes`/`app.router.routes` only exposes the top-level nodes
(so a naive walk finds 0 `APIRoute` instances at the expected, prefixed paths). The real,
effective (fully-prefixed) route list is produced by `fastapi.routing.iter_route_contexts`,
which yields one context per leaf route with `.path` (prefixed), `.methods`, and
`.original_route` (the real `APIRoute`, carrying `.dependant` exactly as the plan describes).
Everything downstream (the `_guard_calls` walk over `.dependant.dependencies`, the `GUARDS`
set, the `EXPECTED` matrix shape, the `PUBLIC` sentinel) is unchanged from the plan.
"""

from __future__ import annotations

from typing import Final

from app.api import deps
from app.main import create_app
from fastapi.routing import APIRoute, iter_route_contexts

# Known guards / session-gates (from `app.api.deps`) that this net inspects. Every
# `Annotated[..., Depends(x)]` alias (`CurrentUser`, `AdminUser`, `TenantSessionDep`, ...)
# resolves to one of these underlying callables, so aliasing doesn't hide anything from
# `_guard_calls` below.
GUARDS: Final = {
    deps.get_current_user,
    deps.get_enrolling_user,
    deps.require_admin,
    deps.require_local_admin,
    deps.require_superadmin,
    deps.require_superadmin_default_context,
    deps.get_tenant_session,
    deps.get_tenant_session_write,
    deps.get_audit_session,
    deps.get_public_tenant_session,
    deps.get_public_tenant_settings_service,
}


class _Public:
    """Distinct sentinel for deliberately public (unauthenticated) routes -- NOT the same as
    `None`/an empty set, so a route missing from `EXPECTED` by accident (forgot to add it)
    can never be confused with one consciously marked public (`assert key in EXPECTED` fails
    the former; `if expected is PUBLIC` only ever matches the latter)."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "PUBLIC"


PUBLIC: Final = _Public()


def _guard_calls(dependant: object) -> set:
    """Recursively flattens `dependant.dependencies` and collects every `.call` that is a
    member of `GUARDS`. Mirrors the plan's sketch 1:1."""
    found: set = set()
    stack = list(dependant.dependencies)  # type: ignore[attr-defined]
    while stack:
        d = stack.pop()
        if d.call in GUARDS:
            found.add(d.call)
        stack.extend(d.dependencies)
    return found


# EXPLICIT expected matrix: (method, path) -> frozenset of required guard callables, or the
# `PUBLIC` sentinel for deliberately public routes. Populated from the ACTUAL router (see
# report) -- health, login, oidc login/callback, 2FA-cookie verify, activity ping, auth
# config, refresh/logout, branding GET (public-tenant-session-gated, not sentinel-public),
# public_tokens accept/info/reset, setup pre-auth (internally self-gated, not via
# `app.api.deps`), health/ready. Every entry below is a conscious decision, not a guess --
# see the Task 5 report for the full route-by-route rationale.
EXPECTED: Final = {
    ("PUT", "/api/admin/assignments/bulk"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("GET", "/api/admin/assignments/{user_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("PUT", "/api/admin/assignments/{user_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("GET", "/api/admin/groups"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("POST", "/api/admin/groups"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("DELETE", "/api/admin/groups/{group_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("PUT", "/api/admin/groups/{group_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("GET", "/api/admin/groups/{group_id}/members"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("POST", "/api/admin/groups/{group_id}/sync"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("PUT", "/api/admin/groups/{group_id}/tenants"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    # GET is intentionally open to EVERY authenticated account (any role) -- the frontend
    # needs the multi-tenant-mode switch state to gate its own chrome regardless of role.
    # PUT (the actual mode switch / default-tenant rename) stays superadmin+default-context.
    ("GET", "/api/admin/instance"): frozenset({deps.get_current_user}),
    ("PUT", "/api/admin/instance"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    # GET is reachable by any authenticated account; the route body itself scopes the
    # returned list via `tenant_repo.allowed_tenant_ids` (superadmin -> all, else own only).
    ("GET", "/api/admin/tenants"): frozenset({deps.get_current_user}),
    ("POST", "/api/admin/tenants"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("DELETE", "/api/admin/tenants/{tenant_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("PATCH", "/api/admin/tenants/{tenant_id}"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    # GET is reachable by any authenticated account; the route body itself default-denies
    # (empty lists) for any role outside admin/superadmin, and further scopes by active tenant.
    ("GET", "/api/admin/users"): frozenset({deps.get_current_user}),
    ("POST", "/api/admin/users"): frozenset({deps.get_current_user, deps.require_admin}),
    ("POST", "/api/admin/users/sso/sync"): frozenset({deps.get_current_user, deps.require_admin}),
    ("POST", "/api/admin/users/superadmin"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("DELETE", "/api/admin/users/{user_id}"): frozenset(
        {deps.get_current_user, deps.require_admin}
    ),
    ("GET", "/api/admin/users/{user_id}/avatar"): frozenset(
        {deps.get_current_user, deps.require_admin}
    ),
    ("POST", "/api/admin/users/{user_id}/reset"): frozenset(
        {deps.get_current_user, deps.require_admin}
    ),
    ("POST", "/api/admin/users/{user_id}/role"): frozenset(
        {deps.get_current_user, deps.require_admin}
    ),
    ("POST", "/api/admin/users/{user_id}/superadmin"): frozenset(
        {deps.get_current_user, deps.require_superadmin, deps.require_superadmin_default_context}
    ),
    ("GET", "/api/audit"): frozenset(
        {deps.get_audit_session, deps.get_current_user, deps.require_admin}
    ),
    ("GET", "/api/audit/actions"): frozenset(
        {deps.get_audit_session, deps.get_current_user, deps.require_admin}
    ),
    ("POST", "/api/auth/2fa/disable"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/2fa/enable"): frozenset({deps.get_enrolling_user}),
    ("POST", "/api/auth/2fa/setup"): frozenset({deps.get_enrolling_user}),
    # 2FA-cookie-gated internally (`decode_token(..., expected_type="2fa")`), not via a
    # tracked `app.api.deps` guard -- part of the pre-full-session login flow.
    ("POST", "/api/auth/2fa/verify"): PUBLIC,
    # Refresh-cookie-gated internally (extends `last_used_at` for idle-timeout tracking);
    # deliberately callable without a full access-token session.
    ("POST", "/api/auth/activity"): PUBLIC,
    ("GET", "/api/auth/config"): PUBLIC,
    ("POST", "/api/auth/language"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/login"): PUBLIC,
    ("POST", "/api/auth/logout"): PUBLIC,
    ("GET", "/api/auth/me"): frozenset({deps.get_current_user}),
    ("DELETE", "/api/auth/me/avatar"): frozenset({deps.get_current_user}),
    ("GET", "/api/auth/me/avatar"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/me/avatar"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/oidc/callback"): PUBLIC,
    ("GET", "/api/auth/oidc/login"): PUBLIC,
    ("POST", "/api/auth/password"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/profile"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/refresh"): PUBLIC,
    ("GET", "/api/auth/sessions"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/sessions/revoke-others"): frozenset({deps.get_current_user}),
    ("POST", "/api/auth/switch-tenant"): frozenset({deps.get_current_user}),
    # Branding GET routes are unauthenticated (pre-login theming) but NOT sentinel-PUBLIC --
    # they run on the dedicated public-tenant-scoped session/settings-service gates.
    ("GET", "/api/branding"): frozenset(
        {deps.get_public_tenant_session, deps.get_public_tenant_settings_service}
    ),
    # Security Phase 5, Task 8/M10 carried `get_tenant_session_write` here (a second, separate
    # `TenantWriteSessionDep` alongside `svc: TenantSettingsDep`) so the new audit entry could
    # be written write-gated. I-01 (Security Audit v0.3.3) then moved `svc` itself onto
    # `TenantWriteSettingsDep` (-> `get_tenant_session_write`, not `get_tenant_session`): the
    # write gate no longer depends on the sibling `session` parameter surviving a future
    # refactor. Net effect on this matrix: `get_tenant_session` (the read gate) drops out of
    # these four -- `get_tenant_session_write` alone now covers both the `svc` and `session`
    # dependency paths, closing the same latent auditor_tenant-grant gap as before but from a
    # second, independent place, matching every sibling tenant-write route (`settings.*`,
    # `users.*`, `notifications.retry`, `runs.trigger`).
    ("DELETE", "/api/branding/favicon"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("GET", "/api/branding/favicon"): frozenset(
        {deps.get_public_tenant_session, deps.get_public_tenant_settings_service}
    ),
    ("POST", "/api/branding/favicon"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("DELETE", "/api/branding/logo"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("GET", "/api/branding/logo"): frozenset(
        {deps.get_public_tenant_session, deps.get_public_tenant_settings_service}
    ),
    ("POST", "/api/branding/logo"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("GET", "/api/dashboard"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("GET", "/api/entra-avatar/{entra_id}"): frozenset(
        {deps.get_current_user, deps.get_tenant_session}
    ),
    ("GET", "/api/notifications"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("POST", "/api/notifications/{log_id}/retry"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("POST", "/api/public/token/accept"): PUBLIC,
    ("GET", "/api/public/token/info"): PUBLIC,
    ("POST", "/api/public/token/reset"): PUBLIC,
    ("GET", "/api/runs"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("POST", "/api/runs/trigger"): frozenset({deps.get_current_user, deps.require_admin}),
    ("GET", "/api/runs/{run_id}"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("GET", "/api/settings"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("PUT", "/api/settings"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("GET", "/api/settings/exclusions"): frozenset(
        {deps.get_current_user, deps.get_tenant_session}
    ),
    ("POST", "/api/settings/exclusions"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("DELETE", "/api/settings/exclusions/{exclusion_id}"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("POST", "/api/settings/graph/test"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("POST", "/api/settings/mail/test"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    # Pure cron-expression computation, no tenant data touched -- CurrentUser only, no
    # tenant-session gate needed.
    ("POST", "/api/settings/schedule/preview"): frozenset({deps.get_current_user}),
    # M4: raised from CurrentUser to AdminUser -- an auditor no longer gets a template preview
    # (reduced attack surface for the one route that renders an untrusted template string).
    ("POST", "/api/settings/template/preview"): frozenset(
        {deps.get_current_user, deps.get_tenant_session, deps.require_admin}
    ),
    ("POST", "/api/settings/template/reset"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    # Pre-auth first-run setup wizard -- internally self-gated (`_require_setup_open_or_admin`,
    # "admin already exists" / "setup already done" checks local to `setup.py`), not via any
    # tracked `app.api.deps` guard. Deliberately public: no admin account exists yet to guard
    # with.
    ("POST", "/api/setup/admin"): PUBLIC,
    ("POST", "/api/setup/database/migrate"): PUBLIC,
    ("POST", "/api/setup/database/test"): PUBLIC,
    ("POST", "/api/setup/graph/test"): PUBLIC,
    ("POST", "/api/setup/mail/test"): PUBLIC,
    ("GET", "/api/setup/status"): PUBLIC,
    ("GET", "/api/users"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("POST", "/api/users/bulk"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("GET", "/api/users/export"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("GET", "/api/users/{user_id}"): frozenset({deps.get_current_user, deps.get_tenant_session}),
    ("POST", "/api/users/{user_id}/exclude"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    ("POST", "/api/users/{user_id}/notify"): frozenset(
        {deps.get_current_user, deps.get_tenant_session_write, deps.require_admin}
    ),
    # Any authenticated account may check for updates (pure GitHub-release comparison, no
    # tenant/customer data) -- not role-restricted.
    ("GET", "/api/version"): frozenset({deps.get_current_user}),
    ("GET", "/health"): PUBLIC,
    ("GET", "/ready"): PUBLIC,
}


def test_every_api_route_matches_the_guard_matrix() -> None:
    app = create_app()
    seen: set[tuple[str, str]] = set()
    for rc in iter_route_contexts(app.routes):
        original = rc.original_route
        if not isinstance(original, APIRoute):
            continue
        methods = (rc.methods or original.methods or set()) - {"HEAD", "OPTIONS"}
        for method in sorted(methods):
            key = (method, rc.path)
            seen.add(key)
            assert key in EXPECTED, (
                f"UNGUARDED/unknown route {key} -- add it to EXPECTED consciously"
            )
            expected = EXPECTED[key]
            guards = _guard_calls(original.dependant)
            if expected is PUBLIC:
                assert not guards, f"{key} is marked PUBLIC but carries guards {guards}"
            else:
                assert guards == expected, f"{key}: guards {guards} != expected {expected}"
    missing = set(EXPECTED) - seen
    assert not missing, f"EXPECTED lists routes that no longer exist: {missing}"
