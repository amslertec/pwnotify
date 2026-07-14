import { useQuery } from '@tanstack/react-query'
import { Clock, History, Wifi } from 'lucide-react'

import { Skeleton } from './ui/skeleton'
import { api } from '@/lib/api'
import { fmtCountdown } from '@/lib/format'
import type { DashboardData } from '@/lib/types'

export function RunStatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    success: 'var(--status-ok)',
    partial: 'var(--status-warn)',
    error: 'var(--status-expired)',
    running: 'var(--color-primary)',
  }
  const label: Record<string, string> = {
    success: 'Erfolgreich',
    partial: 'Teilweise',
    error: 'Fehler',
    running: 'Läuft',
  }
  const color = map[status] ?? 'var(--status-never)'
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {label[status] ?? status}
    </span>
  )
}

function Indicator({ ok, label }: { ok?: boolean; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="size-2.5 rounded-full"
        style={{ background: ok ? 'var(--status-ok)' : 'var(--status-expired)' }}
      />
      <span className={ok ? '' : 'text-muted-foreground'}>{label}</span>
    </div>
  )
}

/** Statusleiste: Graph-/Mail-Verbindung, nächster & letzter Lauf. Lädt selbst. */
export function BackendStatusBar() {
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardData>('/dashboard'),
    refetchInterval: 30_000,
  })

  if (isLoading) return <Skeleton className="h-14 w-full" />

  const graphOk = data?.backends.graph_configured
  const mailOk = data?.backends.mail_configured
  return (
    <div className="border-border bg-card flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border px-4 py-3 text-sm">
      <div className="flex items-center gap-2">
        <Wifi className="text-muted-foreground size-4" />
        <Indicator ok={graphOk} label={graphOk ? 'Graph verbunden' : 'Graph nicht konfiguriert'} />
      </div>
      <Indicator
        ok={mailOk}
        label={mailOk ? `Mail: ${data?.backends.mail_backend}` : 'Mail nicht konfiguriert'}
      />
      <div className="text-muted-foreground flex items-center gap-2">
        <Clock className="size-4" />
        Nächster Lauf:{' '}
        <span className="text-foreground font-medium">{fmtCountdown(data?.next_run)}</span>
      </div>
      {data?.last_run && (
        <div className="text-muted-foreground ml-auto flex items-center gap-2">
          <History className="size-4" />
          Letzter Lauf: <RunStatusPill status={data.last_run.status} />
        </div>
      )}
    </div>
  )
}
