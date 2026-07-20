"""Kanonische Registry aller laufenden App-Einstellungen (DB-basiert).

Jede Einstellung: Default-Wert + ``secret``-Flag. Secrets werden at-rest
Fernet-verschlüsselt und in API-Responses maskiert.
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
from .settings_validators import audit_retention_days, branding_path, number_range


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    secret: bool = False
    validate: Callable[[Any], Any] | None = None


# Dotted keys, gruppiert nach Settings-Tab.
SETTINGS: dict[str, SettingSpec] = {
    # ---- Allgemein ----
    # Öffentliche URL der App (für SSO-Redirect und E-Mail-Links). Leer -> ENV PWNOTIFY_BASE_URL.
    "app.public_url": SettingSpec(""),
    # Prüft periodisch das neueste GitHub-Release und zeigt bei neuerer Version einen Hinweis.
    "app.update_check": SettingSpec(True),
    # Mandantenfähigkeit umschalten (Access-Model-Phase). Default AUS: eine frische wie
    # eine bestehende Instanz bleibt im bisherigen Einzelmandant-Verhalten, bis ein
    # Superadmin bewusst umschaltet. Speicherort (Default-Tenant) + gated Write: Task 5.
    "instance.multi_tenant_mode": SettingSpec(False),
    # ---- Anmeldung ----
    # Zwei-Faktor-Pflicht für LOKALE Konten. Ist sie aktiv, entsteht ohne eingerichtetes
    # 2FA gar keine vollwertige Sitzung: Nach dem Passwort führt der Weg direkt in die
    # Einrichtung. SSO-Konten sind ausgenommen — deren MFA macht Entra selbst.
    "auth.require_2fa": SettingSpec(False),
    # ---- Admin-Benachrichtigungen (Digest + Fehler-Alert) ----
    "alerts.enabled": SettingSpec(False),
    "alerts.recipients": SettingSpec([]),  # Liste von E-Mail-Adressen
    "alerts.digest": SettingSpec(True),  # Zusammenfassung nach jedem geplanten Lauf
    "alerts.on_failure": SettingSpec(True),  # sofortiger Alert bei fehlgeschlagenem Lauf/Versand
    # ---- Graph / Entra ----
    "graph.tenant_id": SettingSpec(""),
    "graph.client_id": SettingSpec(""),
    "graph.client_secret": SettingSpec("", secret=True),
    "graph.cloud": SettingSpec("global"),
    # Ablaufdatum des Client-Secrets (ISO-Datum, z. B. "2027-01-31"), optional.
    # Entra-Secrets laufen nach 6-24 Monaten ab; danach steht das Tool still, ohne
    # dass vorher jemand gewarnt wurde. Das Datum wird bewusst manuell gepflegt:
    # automatisch auslesen ginge nur mit Application.Read.All — einer Berechtigung,
    # die ALLE App-Registrierungen des Tenants lesen darf. Das ist für eine reine
    # Warnung unverhältnismässig.
    "graph.client_secret_expires_at": SettingSpec(""),
    # Objekt-ID einer Entra-Gruppe: nur deren Mitglieder werden synchronisiert und auf
    # Passwortablauf geprüft. Leer -> alle Tenant-Benutzer (altes Verhalten).
    # transitiveMembers löst verschachtelte Gruppen auf; ideal mit einer dynamischen Gruppe.
    "sync.group_id": SettingSpec(""),
    # ---- SSO / OIDC (Anmeldung mit Microsoft-Konto) ----
    # Nutzt dieselbe App-Registrierung (Tenant/Client/Secret) wie Graph.
    "oidc.enabled": SettingSpec(False),
    # Entra-Gruppen-Objekt-ID: Mitglieder erhalten die Admin-Rolle (voller Zugriff).
    "oidc.admin_group_id": SettingSpec(""),
    # Optional: Mitglieder dieser Gruppe erhalten die Auditor-Rolle (read-only).
    "oidc.auditor_group_id": SettingSpec(""),
    # Beschriftung des SSO-Buttons auf der Login-Seite.
    "oidc.button_label": SettingSpec("Mit Microsoft anmelden"),
    # ---- Mail ----
    "mail.backend": SettingSpec("graph"),  # graph | smtp
    "mail.from": SettingSpec(""),
    "mail.recipient_strategy": SettingSpec("primary"),
    "mail.smtp_host": SettingSpec(""),
    "mail.smtp_port": SettingSpec(587),
    "mail.smtp_username": SettingSpec(""),
    "mail.smtp_password": SettingSpec("", secret=True),
    "mail.smtp_tls": SettingSpec("starttls"),  # starttls | ssl | none
    # ---- Schedule ----
    "schedule.cron": SettingSpec("0 8 * * *"),
    "schedule.timezone": SettingSpec("Europe/Zurich"),
    "schedule.reminder_days": SettingSpec([14, 7, 3, 1, 0]),
    "schedule.dry_run": SettingSpec(False),
    # Aufbewahrung des Audit-Protokolls in Tagen. 0 = unbegrenzt (Standard) — für
    # Compliance ist eine lückenlose Historie meist erwünscht. Wer eine Löschfrist
    # braucht (Datenschutz), setzt hier z. B. 365; ältere Einträge werden nach jedem
    # geplanten Lauf entfernt. Untergrenze (M3): 0 (unbegrenzt) ODER >= FLOOR Tage — ein
    # sub-FLOOR-Fenster (1..FLOOR-1) wird abgelehnt, damit das Protokoll nicht in kleinen
    # Schritten leergeräumt werden kann.
    "audit.retention_days": SettingSpec(0, validate=audit_retention_days),
    # ---- Aufbewahrung personenbezogener Daten (alle 0 = unbegrenzt) ----
    # Entra-Konten, die seit so vielen Tagen nicht mehr im Sync auftauchen, gelten als
    # ausgeschieden und werden entfernt. Ohne Frist bleiben Name, UPN und Mailadressen
    # von Personen gespeichert, die den Tenant längst verlassen haben.
    # Ein Sanity-Schutz verhindert, dass ein tagelang fehlgeschlagener Sync — nach dem
    # alle Einträge gleich alt wirken — den Bestand leerräumt.
    # L4: Validator wie audit.retention_days — ein negativer oder nicht-numerischer Wert
    # würde die Frist über `int(... or 0)` sonst still deaktivieren.
    "privacy.user_retention_days": SettingSpec(
        0, validate=number_range(min_value=0, integer_only=True)
    ),
    # Versandhistorie (notification_log) und Lauf-Protokolle älter als X Tage entfernen.
    # Beide enthalten UPNs und Empfängeradressen.
    "privacy.log_retention_days": SettingSpec(
        0, validate=number_range(min_value=0, integer_only=True)
    ),
    # Sicherung gegen Massenversand: Würde ein Lauf mehr als diesen Anteil aller
    # geprüften Benutzer benachrichtigen, ist das fast immer eine Fehlkonfiguration
    # (z. B. falsche Gültigkeitsdauer) und nicht ein realer Stichtag. Der Lauf bricht
    # dann ab, statt tausende Mails zu verschicken. Bereich (0, 1] — 0 ist gesperrt,
    # die Bremse lässt sich nicht mehr abschalten (siehe schedule.max_notify_count
    # für die zweite, absolute Bremse).
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
    # ---- Shared-Mailbox-Erkennung ----
    # Primär: Konto hat ein Postfach, aber keine Lizenz -> Shared/Room/Equipment.
    "sync.shared_detect_unlicensed": SettingSpec(True),
    # Zusätzlich (optional): Glob-Muster gegen UPN/primäre Mail als manueller Override.
    "sync.shared_patterns": SettingSpec(
        ["noreply@*", "no-reply@*", "donotreply@*", "do-not-reply@*"]
    ),
    # ---- Branding ----
    "branding.app_name": SettingSpec("PwNotify"),
    "branding.company_name": SettingSpec(""),
    "branding.primary_color": SettingSpec("#4F46E5"),
    "branding.logo_path": SettingSpec(None, validate=branding_path),
    "branding.favicon_path": SettingSpec(None, validate=branding_path),
    "branding.reset_url": SettingSpec(
        "https://account.activedirectory.windowsazure.com/ChangePassword.aspx"
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
    # ---- Template: Einladung + Passwort-Reset (Task 5) ----
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
MASK = "__SECRET_SET__"  # Marker im Frontend: "gesetzt, nicht anzeigbar"


def default_settings() -> dict[str, Any]:
    return {k: spec.default for k, spec in SETTINGS.items()}
