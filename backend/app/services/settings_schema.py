"""Canonical registry of all runtime app settings (DB-based).

Each setting: default value + ``secret`` flag. Secrets are Fernet-encrypted
at rest and masked in API responses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .default_templates import (
    DEFAULT_HTML_DE,
    DEFAULT_HTML_EN,
    DEFAULT_HTML_INVITE_DE,
    DEFAULT_HTML_INVITE_EN,
    DEFAULT_HTML_RESET_DE,
    DEFAULT_HTML_RESET_EN,
    DEFAULT_SUBJECT_DE,
    DEFAULT_SUBJECT_EN,
    DEFAULT_SUBJECT_INVITE_DE,
    DEFAULT_SUBJECT_INVITE_EN,
    DEFAULT_SUBJECT_RESET_DE,
    DEFAULT_SUBJECT_RESET_EN,
    DEFAULT_TEXT_DE,
    DEFAULT_TEXT_EN,
    DEFAULT_TEXT_INVITE_DE,
    DEFAULT_TEXT_INVITE_EN,
    DEFAULT_TEXT_RESET_DE,
    DEFAULT_TEXT_RESET_EN,
)
from .settings_validators import (
    audit_retention_days,
    branding_path,
    number_range,
    smtp_host,
    url_setting,
)


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    secret: bool = False
    validate: Callable[[Any], Any] | None = None


# Dotted keys, grouped by settings tab.
SETTINGS: dict[str, SettingSpec] = {
    # ---- General ----
    # Public URL of the app (for SSO redirect and email links). Empty -> ENV PWNOTIFY_BASE_URL.
    # A7: url_setting enforces https + forbids dangerous schemes/CRLF — the value feeds the
    # one-time token links of outgoing reset/invite emails (effective_base_url).
    "app.public_url": SettingSpec("", validate=url_setting),
    # Periodically checks the latest GitHub release and shows a notice if a newer version exists.
    "app.update_check": SettingSpec(True),
    # Toggle multi-tenancy (access-model phase). Default OFF: a fresh instance as well as an
    # existing one stays on the previous single-tenant behavior until a superadmin
    # deliberately switches it. Storage location (default tenant) + gated write: Task 5.
    "instance.multi_tenant_mode": SettingSpec(False),
    # ---- Login ----
    # Two-factor requirement for LOCAL accounts. When active, no full session is created
    # without 2FA set up: after the password, the flow leads straight into setup.
    # SSO accounts are exempt — their MFA is handled by Entra itself.
    "auth.require_2fa": SettingSpec(False),
    # ---- Admin notifications (digest + failure alert) ----
    "alerts.enabled": SettingSpec(False),
    "alerts.recipients": SettingSpec([]),  # list of email addresses
    "alerts.digest": SettingSpec(True),  # summary after every scheduled run
    "alerts.on_failure": SettingSpec(True),  # immediate alert on a failed run/send
    # ---- Graph / Entra ----
    "graph.tenant_id": SettingSpec(""),
    "graph.client_id": SettingSpec(""),
    "graph.client_secret": SettingSpec("", secret=True),
    "graph.cloud": SettingSpec("global"),
    # Client secret expiry date (ISO date, e.g. "2027-01-31"), optional.
    # Entra secrets expire after 6-24 months; after that the tool stops working without
    # anyone having been warned beforehand. The date is deliberately maintained manually:
    # reading it automatically would require Application.Read.All — a permission that
    # can read ALL app registrations of the tenant. That's disproportionate for a mere
    # warning.
    "graph.client_secret_expires_at": SettingSpec(""),
    # Object ID of an Entra group: only its members are synced and checked for
    # password expiry. Empty -> all tenant users (previous behavior).
    # transitiveMembers resolves nested groups; ideal with a dynamic group.
    "sync.group_id": SettingSpec(""),
    # Test mode: when on, the notification filter ALSO includes disabled
    # (account_enabled=false) and unlicensed (is_shared=true) accounts -- they receive REAL
    # reminder mails, to exercise the send/expiry flow. Per tenant (no `instance.` prefix,
    # like the other sync.* keys). The excluded/expiry_date filters and the mass-send brake
    # stay in force. Default off.
    "sync.test_mode": SettingSpec(False),
    # ---- SSO / OIDC (login with Microsoft account) ----
    # Uses the same app registration (tenant/client/secret) as Graph.
    "oidc.enabled": SettingSpec(False),
    # Entra group object ID: members receive the admin role (full access).
    "oidc.admin_group_id": SettingSpec(""),
    # Optional: members of this group receive the auditor role (read-only).
    "oidc.auditor_group_id": SettingSpec(""),
    # Label of the SSO button on the login page.
    "oidc.button_label": SettingSpec("Mit Microsoft anmelden"),
    # ---- Mail ----
    "mail.backend": SettingSpec("graph"),  # graph | smtp
    "mail.from": SettingSpec(""),
    "mail.recipient_strategy": SettingSpec("primary"),
    # A6: smtp_host rejects internal/link-local/RFC1918 targets (blind SSRF), unless explicitly
    # allowed via PWNOTIFY_SMTP_ALLOWED_HOSTS. tls=none cross-check: SettingsService.set_many.
    "mail.smtp_host": SettingSpec("", validate=smtp_host),
    "mail.smtp_port": SettingSpec(587),
    "mail.smtp_username": SettingSpec(""),
    "mail.smtp_password": SettingSpec("", secret=True),
    "mail.smtp_tls": SettingSpec("starttls"),  # starttls | ssl | none
    # ---- Schedule ----
    "schedule.cron": SettingSpec("0 8 * * *"),
    "schedule.timezone": SettingSpec("Europe/Zurich"),
    "schedule.reminder_days": SettingSpec([14, 7, 3, 1, 0]),
    "schedule.dry_run": SettingSpec(False),
    # Retention of the audit log in days. 0 = unlimited (default) — for compliance an
    # unbroken history is usually desired. Anyone who needs a deletion period (privacy)
    # sets e.g. 365 here; older entries are removed after every scheduled run. Floor
    # (M3): 0 (unlimited) OR >= FLOOR days — a sub-FLOOR window (1..FLOOR-1) is rejected
    # so the log can't be emptied out in small increments.
    "audit.retention_days": SettingSpec(0, validate=audit_retention_days),
    # ---- Retention of personal data (all 0 = unlimited) ----
    # Entra accounts that haven't shown up in the sync for this many days are considered
    # departed and are removed. Without a deadline, the name, UPN, and email addresses
    # of people who left the tenant long ago would remain stored.
    # A sanity guard prevents a sync that has failed for days — after which all entries
    # look equally old — from wiping out the whole dataset.
    # L4: validator like audit.retention_days — a negative or non-numeric value would
    # otherwise silently disable the deadline via `int(... or 0)`.
    "privacy.user_retention_days": SettingSpec(
        0, validate=number_range(min_value=0, integer_only=True)
    ),
    # Remove send history (notification_log) and run logs older than X days.
    # Both contain UPNs and recipient addresses.
    "privacy.log_retention_days": SettingSpec(
        0, validate=number_range(min_value=0, integer_only=True)
    ),
    # Safeguard against mass mailings: if a run would notify more than this fraction of
    # all checked users, that is almost always a misconfiguration (e.g. wrong validity
    # duration) rather than a real deadline. The run then aborts instead of sending
    # thousands of emails. Range (0, 1] — 0 is disallowed, so this brake can never be
    # switched off entirely (see schedule.max_notify_count for the second, absolute
    # brake).
    "schedule.max_notify_ratio": SettingSpec(
        0.5, validate=number_range(min_value=0, exclusive_min=True, max_value=1.0)
    ),
    # Absolute, non-disable-able ceiling: even if the ratio brake would pass (e.g. a huge
    # tenant), never send more than this many notifications in one run without an admin
    # deliberately raising it. Second line of defence behind max_notify_ratio.
    "schedule.max_notify_count": SettingSpec(
        500, validate=number_range(min_value=1, integer_only=True)
    ),
    # ---- Password Policy ----
    "policy.auto_detect": SettingSpec(True),
    "policy.validity_days_override": SettingSpec(None),
    # ---- Shared mailbox detection ----
    # Primary: account has a mailbox but no license -> Shared/Room/Equipment.
    "sync.shared_detect_unlicensed": SettingSpec(True),
    # Additionally (optional): glob patterns against UPN/primary mail as a manual override.
    "sync.shared_patterns": SettingSpec(
        ["noreply@*", "no-reply@*", "donotreply@*", "do-not-reply@*"]
    ),
    # ---- Branding ----
    "branding.app_name": SettingSpec("PwNotify"),
    "branding.company_name": SettingSpec(""),
    "branding.primary_color": SettingSpec("#4F46E5"),
    "branding.logo_path": SettingSpec(None, validate=branding_path),
    "branding.favicon_path": SettingSpec(None, validate=branding_path),
    # A7: url_setting like app.public_url — the reset URL is embedded in outgoing emails.
    "branding.reset_url": SettingSpec(
        "https://account.activedirectory.windowsazure.com/ChangePassword.aspx",
        validate=url_setting,
    ),
    # ---- Template ----
    "template.language_default": SettingSpec("de"),  # de | en
    "template.language_per_user": SettingSpec(True),
    "template.subject_de": SettingSpec(DEFAULT_SUBJECT_DE),
    "template.subject_en": SettingSpec(DEFAULT_SUBJECT_EN),
    "template.html_de": SettingSpec(DEFAULT_HTML_DE),
    "template.html_en": SettingSpec(DEFAULT_HTML_EN),
    "template.text_de": SettingSpec(DEFAULT_TEXT_DE),
    "template.text_en": SettingSpec(DEFAULT_TEXT_EN),
    # ---- Template: invitation + password reset (Task 5) ----
    "template.invite_subject_de": SettingSpec(DEFAULT_SUBJECT_INVITE_DE),
    "template.invite_subject_en": SettingSpec(DEFAULT_SUBJECT_INVITE_EN),
    "template.invite_html_de": SettingSpec(DEFAULT_HTML_INVITE_DE),
    "template.invite_html_en": SettingSpec(DEFAULT_HTML_INVITE_EN),
    "template.invite_text_de": SettingSpec(DEFAULT_TEXT_INVITE_DE),
    "template.invite_text_en": SettingSpec(DEFAULT_TEXT_INVITE_EN),
    "template.reset_subject_de": SettingSpec(DEFAULT_SUBJECT_RESET_DE),
    "template.reset_subject_en": SettingSpec(DEFAULT_SUBJECT_RESET_EN),
    "template.reset_html_de": SettingSpec(DEFAULT_HTML_RESET_DE),
    "template.reset_html_en": SettingSpec(DEFAULT_HTML_RESET_EN),
    "template.reset_text_de": SettingSpec(DEFAULT_TEXT_RESET_DE),
    "template.reset_text_en": SettingSpec(DEFAULT_TEXT_RESET_EN),
}

SECRET_KEYS = {k for k, spec in SETTINGS.items() if spec.secret}
MASK = "__SECRET_SET__"  # marker for the frontend: "set, not displayable"


def default_settings() -> dict[str, Any]:
    return {k: spec.default for k, spec in SETTINGS.items()}
