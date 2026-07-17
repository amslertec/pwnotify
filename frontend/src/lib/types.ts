export interface TenantRef {
  id: number
  name: string
}

export interface User {
  id: number
  username: string
  display_name: string | null
  is_sso: boolean
  role: string
  language: string
  two_factor_enabled: boolean
  last_login_at: string | null
  has_avatar: boolean
  avatar_version: number
  /** Minuten ohne Aktivität bis zur automatischen Abmeldung (0 = aus). */
  idle_timeout_min: number
  /** Aktuell aktiver Kunde (Mandant) — null, falls (noch) keinem zugeordnet. */
  active_tenant: TenantRef | null
  /** Kunden, zu denen dieses Konto wechseln darf. */
  switchable_tenants: TenantRef[]
  /** Instanzweiter Schalterstand (Access-Modell/Superadmin-Phase) — steuert, ob die
   *  Mandantenfähigkeit (Kunden-Umschalter, Zuweisungen) überhaupt aktiv ist. */
  multi_tenant_mode: boolean
  /** True nur, wenn der aktive Mandant des Aufrufers der Standard-/Provider-Kunde ist
   *  (Context-Gating v2, Task 4/5). Steuert, ob provider-only Oberflächen (Kunden-Konsole,
   *  Modus-Schalter, Settings-General-Tab) sichtbar sind — auch für einen Superadmin,
   *  der gerade in einen Kunden-Kontext gewechselt hat. */
  active_tenant_is_default: boolean
  /** E-Mail-Adresse (Console+Groups+Invite Task 5/8) — bei lokalen Konten selbst pflegbar
   *  (`POST /auth/profile`), bei SSO-Konten schreibgeschützt (kommt aus Entra). Der
   *  Reset-Trigger (Access-Seite) verschickt genau an diese Adresse. */
  email: string | null
}

export interface LoginResponse {
  two_factor_required: boolean
  /** 2FA ist Pflicht, aber noch nicht eingerichtet — es gibt noch keine Sitzung. */
  two_factor_setup_required: boolean
  user: User | null
}

export interface TwoFactorSetup {
  otpauth_uri: string
  qr_png: string
  secret: string
}

export interface VersionInfo {
  current: string
  latest: string | null
  update_available: boolean
  release_url: string
  release_name: string | null
  notes: string | null
  published_at: string | null
  checked_at: string | null
  enabled: boolean
}

export interface AdminUser {
  id: number
  username: string
  display_name: string | null
  is_sso: boolean
  is_active: boolean
  role: string
  last_login_at: string | null
  created_at: string
}

export interface AdminUsers {
  local: AdminUser[]
  sso: AdminUser[]
  /** Nur vorhanden, wenn der Aufrufer selbst Superadmin ist (Access-Modell/Superadmin-Phase). */
  superadmins?: AdminUser[]
}

/** Zuweisungsstand eines Admin-/Auditor-Kontos (`GET`/`PUT /admin/assignments/{id}`) —
 *  Superadmin-only. `role` spiegelt die Rolle des Zielkontos, `tenant_ids` die aktuell
 *  gehaltenen Kunden-Zuweisungen (Grant-Typ wird serverseitig aus `role` abgeleitet). */
export interface Assignment {
  role: string
  tenant_ids: number[]
}

/** Mandant (Kunde) — Phase 4c Kundenverwaltung. Nicht zu verwechseln mit `TenantRef`
 *  (schlanke Referenz auf dem `User`), das ist der volle Verwaltungsdatensatz. */
export interface Tenant {
  id: number
  name: string
  slug: string
  entra_tenant_id: string | null
  is_active: boolean
  created_at: string
  /** Anzahl per SSO an diesen Kunden gebundener Konten. */
  sso_user_count: number
}

/** Team/Gruppe (Console+Groups+Invite-Phase, Task 3/7): Entra-Security-Gruppe des
 *  Provider-Tenants, gemappt auf einen oder mehrere Kunden (`GET`/`POST`/`PUT`/`DELETE
 *  /admin/groups*`). Mitgliedschaft wird in Entra gepflegt -- hier nur die Kunden-Zuordnung. */
export interface AssignmentGroup {
  id: number
  name: string
  entra_group_id: string
  tenant_ids: number[]
  /** Anzahl aktuell materialisierter Mitglieder (Group-Member-Sync, Task 4/5). */
  member_count: number
  /** Zeitpunkt des letzten erfolgreichen Sync -- null, wenn noch nie synchronisiert. */
  last_synced_at: string | null
}

/** Einzelnes Gruppenmitglied (`GET /admin/groups/{id}/members`, Group-Member-Sync Task 4/5). */
export interface GroupMember {
  entra_id: string
  upn: string
  display_name: string | null
  mail: string | null
}

/** Paginierte Mitgliederliste. `page` ist 1-basiert; `size` muss <= 200 sein (Server
 *  antwortet sonst mit 422) -- das Frontend sendet immer `size=25`. Eine Seite ausserhalb
 *  des Bereichs liefert leere `items` bei korrektem `total`, nie einen Fehler. */
export interface GroupMemberPage {
  items: GroupMember[]
  total: number
  page: number
  size: number
}

/** Ergebnis eines Gruppen-Sync (`POST /admin/groups/{id}/sync`). Bei Graph-Fehler antwortet
 *  die Route stattdessen mit HTTP 502 / `{code: "sync_failed", ...}`. */
export interface GroupSyncResult {
  member_count: number
  materialized: number
  added: number
  removed: number
}

/** Bulk-Zuweisung mehrerer Konten auf mehrere Kunden in einem Rutsch (`PUT
 *  /admin/assignments/bulk`, Task 2/7). `action` bestimmt die Semantik: `add`/`remove`
 *  ergänzen bzw. entfernen `tenant_ids` bei der bestehenden Zuweisung je Konto, `set`
 *  ersetzt sie vollständig. */
export interface BulkAssignment {
  user_ids: number[]
  tenant_ids: number[]
  action: 'add' | 'remove' | 'set'
}

/** Ergebnis einer Bulk-Zuweisung: `updated` sind erfolgreich geänderte Konto-IDs,
 *  `skipped` listet übersprungene Konten mit Grund (`reason` ∈ `customer_account_not_grantable`
 *  | `cannot_assign_superadmin` | `user_not_found`, s. Backend `bulk_assign`). */
export interface BulkAssignmentResult {
  updated: number[]
  skipped: { user_id: number; reason: string }[]
}

export interface Session {
  id: number
  user_agent: string | null
  ip_address: string | null
  created_at: string
  last_used_at: string
  current: boolean
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface EntraUser {
  id: number
  entra_id: string
  upn: string
  display_name: string
  mail: string | null
  other_mails: string[]
  account_enabled: boolean
  department: string | null
  job_title: string | null
  language: string | null
  last_password_change: string | null
  password_policies: string | null
  password_never_expires: boolean
  expiry_date: string | null
  days_left: number | null
  excluded: boolean
  is_shared: boolean
  last_synced_at: string
}

export interface EntraUserDetail extends EntraUser {
  raw: Record<string, unknown>
}

export interface Notification {
  id: number
  entra_user_id: number
  run_id: number | null
  reminder_day: number
  expiry_cycle: string
  channel: string
  backend: string
  recipient: string
  language: string
  status: string
  error: string | null
  created_at: string
}

export interface Run {
  id: number
  trigger: string
  status: string
  dry_run: boolean
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  checked_users: number
  sent: number
  failed: number
  skipped: number
  error: string | null
}

export interface RunDetail extends Run {
  detail_log: Array<Record<string, unknown>>
}

export interface Exclusion {
  id: number
  kind: string
  value: string
  label: string | null
  created_at: string
}

export interface AuthConfig {
  oidc_enabled: boolean
  oidc_button_label: string
}

export interface SetupStatus {
  needs_setup: boolean
  has_admin: boolean
  database_ready: boolean
  graph_configured: boolean
  mail_configured: boolean
}

export interface GraphTestResult {
  connected: boolean
  tenant_id: string | null
  granted_permissions: string[]
  missing_permissions: string[]
  error: string | null
}

export interface PublicBranding {
  app_name: string
  company_name: string
  primary_color: string
  reset_url: string
  has_logo: boolean
  has_favicon: boolean
  logo_version: number
  favicon_version: number
}

export type Settings = Record<string, unknown>

/** Instanzweiter Schalterstand + Name des Standard-Kunden (Access-Modell/Superadmin-Phase,
 *  Task 7) — `GET`/`PUT /admin/instance`, superadmin-only zum Schreiben. Getrennt von
 *  `Settings`, weil diese Werte den Standard-Kunden betreffen, nicht den aktiven Mandanten. */
export interface InstanceSettings {
  multi_tenant_mode: boolean
  default_tenant_name: string
}

export interface DashboardData {
  kpis: {
    total: number
    expiring_soon: number
    expired: number
    never: number
    disabled: number
    mails_today: number
  }
  status_distribution: Array<{ status: string; count: number }>
  expiry_histogram: Array<{ date: string; count: number }>
  top_upcoming: EntraUser[]
  last_run: Run | null
  next_run: string | null
  backends: { graph_configured: boolean; mail_configured: boolean; mail_backend: string }
  /** Nur gesetzt, wenn das Graph-Secret bald ablaeuft oder abgelaufen ist. */
  secret_expiry: { expires_at: string; days_left: number; expired: boolean } | null
}

export interface AuditEntry {
  id: number
  at: string
  actor_username: string | null
  actor_type: string
  /** Stabile Kennung wie "user.role_changed" — wird im Frontend uebersetzt. */
  action: string
  target: string | null
  outcome: string
  ip_address: string | null
  user_agent: string | null
  detail: Record<string, unknown>
}

/** Zweck eines öffentlichen Einmal-Tokens (`GET /public/token/info`, Task 8). */
export type TokenPurpose = 'invite' | 'reset'

/** Antwort auf `GET /public/token/info?token=&purpose=` — bei JEDEM ungültigen Zustand
 *  (nie existiert, falscher purpose, abgelaufen, bereits verbraucht) IMMER
 *  `valid=false, email=null, purpose=null`. Niemals ein Hinweis, ob ein Token/Konto je
 *  existiert hat (keine Enumeration) — s. Backend `schemas/auth.py::TokenInfo`. */
export interface TokenInfo {
  valid: boolean
  email: string | null
  purpose: TokenPurpose | null
}

/** Body für `POST /public/token/accept` (Einladung annehmen). Kein `email`-Feld: die
 *  Zieladresse ist bereits über das Token an das Konto gebunden. */
export interface TokenAcceptRequest {
  token: string
  first_name: string
  last_name: string
  username: string
  password: string
}

/** Body für `POST /public/token/reset`. Bewusst kein `username` — das Konto ist über das
 *  Token bereits fixiert. */
export interface TokenResetRequest {
  token: string
  password: string
}

export interface AuditPage {
  items: AuditEntry[]
  total: number
  page: number
  page_size: number
}
