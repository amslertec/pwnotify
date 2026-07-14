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
}

export interface LoginResponse {
  two_factor_required: boolean
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
}
