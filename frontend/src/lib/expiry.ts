import type { EntraUser } from './types'

export type ExpiryStatus = 'ok' | 'warn' | 'soon' | 'expired' | 'never' | 'disabled'

export const STATUS_META: Record<ExpiryStatus, { label: string; varName: string }> = {
  ok: { label: 'OK', varName: 'var(--status-ok)' },
  warn: { label: 'Bald', varName: 'var(--status-warn)' },
  soon: { label: 'Kritisch', varName: 'var(--status-soon)' },
  expired: { label: 'Abgelaufen', varName: 'var(--status-expired)' },
  never: { label: 'Kein Ablauf', varName: 'var(--status-never)' },
  disabled: { label: 'Deaktiviert', varName: 'var(--status-never)' },
}

/** Farbcodierung: grün >14, gelb 7–14, orange 1–6, rot <=0, grau = kein Ablauf. */
export function expiryStatus(u: Pick<EntraUser, 'days_left' | 'account_enabled'>): ExpiryStatus {
  if (!u.account_enabled) return 'disabled'
  if (u.days_left == null) return 'never'
  if (u.days_left <= 0) return 'expired'
  if (u.days_left <= 6) return 'soon'
  if (u.days_left <= 14) return 'warn'
  return 'ok'
}

export function daysLeftLabel(days: number | null): string {
  if (days == null) return '—'
  if (days === 0) return 'Heute'
  if (days < 0) return `vor ${Math.abs(days)} T`
  return `${days} T`
}
