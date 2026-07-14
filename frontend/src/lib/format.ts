import { format, formatDistanceToNowStrict, parseISO } from 'date-fns'
import { de } from 'date-fns/locale'

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return format(parseISO(iso), 'dd.MM.yyyy', { locale: de })
  } catch {
    return '—'
  }
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return format(parseISO(iso), 'dd.MM.yyyy HH:mm', { locale: de })
  } catch {
    return '—'
  }
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return formatDistanceToNowStrict(parseISO(iso), { locale: de, addSuffix: true })
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
    if (diff <= 0) return 'jetzt'
    const mins = Math.floor(diff / 60000)
    const h = Math.floor(mins / 60)
    const d = Math.floor(h / 24)
    if (d > 0) return `in ${d} T ${h % 24} h`
    if (h > 0) return `in ${h} h ${mins % 60} min`
    return `in ${mins} min`
  } catch {
    return '—'
  }
}
