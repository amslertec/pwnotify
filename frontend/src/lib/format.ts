import { format, formatDistanceToNowStrict, parseISO } from 'date-fns'
import { de, enUS } from 'date-fns/locale'

import i18n from '@/i18n'

/** date-fns-Locale passend zur aktiven UI-Sprache. */
function dfLocale() {
  return i18n.resolvedLanguage === 'en' ? enUS : de
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return format(parseISO(iso), 'dd.MM.yyyy', { locale: dfLocale() })
  } catch {
    return '—'
  }
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return format(parseISO(iso), 'dd.MM.yyyy HH:mm', { locale: dfLocale() })
  } catch {
    return '—'
  }
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return formatDistanceToNowStrict(parseISO(iso), { locale: dfLocale(), addSuffix: true })
  } catch {
    return '—'
  }
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s % 60)}s`
}

/** Countdown-Text bis zu einem ISO-Zeitpunkt (für „nächster Lauf"). */
export function fmtCountdown(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const diff = parseISO(iso).getTime() - Date.now()
    if (diff <= 0) return i18n.t('time.now')
    const mins = Math.floor(diff / 60000)
    const h = Math.floor(mins / 60)
    const d = Math.floor(h / 24)
    if (d > 0) return i18n.t('time.inDaysHours', { d, h: h % 24 })
    if (h > 0) return i18n.t('time.inHoursMins', { h, m: mins % 60 })
    return i18n.t('time.inMins', { m: mins })
  } catch {
    return '—'
  }
}
