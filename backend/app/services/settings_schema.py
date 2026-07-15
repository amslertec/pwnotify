"""Kanonische Registry aller laufenden App-Einstellungen (DB-basiert).

Jede Einstellung: Default-Wert + ``secret``-Flag. Secrets werden at-rest
Fernet-verschlüsselt und in API-Responses maskiert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .default_templates import (
    DEFAULT_HTML_DE,
    DEFAULT_HTML_EN,
    DEFAULT_SUBJECT_DE,
    DEFAULT_SUBJECT_EN,
    DEFAULT_TEXT_DE,
    DEFAULT_TEXT_EN,
)


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    secret: bool = False


# Dotted keys, gruppiert nach Settings-Tab.
SETTINGS: dict[str, SettingSpec] = {
    # ---- Allgemein ----
    # Öffentliche URL der App (für SSO-Redirect und E-Mail-Links). Leer -> ENV PWNOTIFY_BASE_URL.
    "app.public_url": SettingSpec(""),
    # Prüft periodisch das neueste GitHub-Release und zeigt bei neuerer Version einen Hinweis.
    "app.update_check": SettingSpec(True),
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
    # geplanten Lauf entfernt.
    "audit.retention_days": SettingSpec(0),
    # ---- Aufbewahrung personenbezogener Daten (alle 0 = unbegrenzt) ----
    # Entra-Konten, die seit so vielen Tagen nicht mehr im Sync auftauchen, gelten als
    # ausgeschieden und werden entfernt. Ohne Frist bleiben Name, UPN und Mailadressen
    # von Personen gespeichert, die den Tenant längst verlassen haben.
    # Ein Sanity-Schutz verhindert, dass ein tagelang fehlgeschlagener Sync — nach dem
    # alle Einträge gleich alt wirken — den Bestand leerräumt.
    "privacy.user_retention_days": SettingSpec(0),
    # Versandhistorie (notification_log) und Lauf-Protokolle älter als X Tage entfernen.
    # Beide enthalten UPNs und Empfängeradressen.
    "privacy.log_retention_days": SettingSpec(0),
    # Sicherung gegen Massenversand: Würde ein Lauf mehr als diesen Anteil aller
    # geprüften Benutzer benachrichtigen, ist das fast immer eine Fehlkonfiguration
    # (z. B. falsche Gültigkeitsdauer) und nicht ein realer Stichtag. Der Lauf bricht
    # dann ab, statt tausende Mails zu verschicken. 0 = Sicherung aus.
    "schedule.max_notify_ratio": SettingSpec(0.5),
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
    "branding.logo_path": SettingSpec(None),
    "branding.favicon_path": SettingSpec(None),
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
}

SECRET_KEYS = {k for k, spec in SETTINGS.items() if spec.secret}
MASK = "__SECRET_SET__"  # Marker im Frontend: "gesetzt, nicht anzeigbar"


def default_settings() -> dict[str, Any]:
    return {k: spec.default for k, spec in SETTINGS.items()}
