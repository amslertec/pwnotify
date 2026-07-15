import { useQuery } from '@tanstack/react-query'
import { Clock, History, KeyRound, Wifi } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { Skeleton } from './ui/skeleton'
import { api } from '@/lib/api'
import { fmtCountdown, fmtDate } from '@/lib/format'
import type { DashboardData } from '@/lib/types'

export function RunStatusPill({ status }: { status: string }) {
  const { t } = useTranslation()
  const map: Record<string, string> = {
    success: 'var(--status-ok)',
    partial: 'var(--status-warn)',
    error: 'var(--status-expired)',
    running: 'var(--color-primary)',
  }
  const label: Record<string, string> = {
    success: t('backendStatus.runStatus.success'),
    partial: t('backendStatus.runStatus.partial'),
    error: t('backendStatus.runStatus.error'),
    running: t('backendStatus.runStatus.running'),
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
  const { t } = useTranslation()
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardData>('/dashboard'),
    refetchInterval: 30_000,
  })

  if (isLoading) return <Skeleton className="h-14 w-full" />

  const graphOk = data?.backends.graph_configured
  const mailOk = data?.backends.mail_configured
  const secret = data?.secret_expiry
  return (
    <div className="space-y-2">
      {secret && (
        // Ein ablaufendes Secret legt das Tool still, ohne dass es auffiele — deshalb
        // steht der Hinweis ueber dem Status und nicht darin.
        <div className="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-lg border px-4 py-3 text-sm">
          <KeyRound className="mt-0.5 size-4 shrink-0" />
          <span>
            {secret.expired
              ? t('backendStatus.secretExpired', { date: fmtDate(secret.expires_at) })
              : t('backendStatus.secretSoon', {
                  count: secret.days_left,
                  date: fmtDate(secret.expires_at),
                })}
          </span>
        </div>
      )}
      <div className="border-border bg-card flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border px-4 py-3 text-sm">
      <div className="flex items-center gap-2">
        <Wifi className="text-muted-foreground size-4" />
        <Indicator
          ok={graphOk}
          label={
            graphOk ? t('backendStatus.graph.connected') : t('backendStatus.graph.notConfigured')
          }
        />
      </div>
      <Indicator
        ok={mailOk}
        label={
          mailOk
            ? t('backendStatus.mail.configured', { backend: data?.backends.mail_backend })
            : t('backendStatus.mail.notConfigured')
        }
      />
      <div className="text-muted-foreground flex items-center gap-2">
        <Clock className="size-4" />
        {t('backendStatus.nextRun')}{' '}
        <span className="text-foreground font-medium">{fmtCountdown(data?.next_run)}</span>
      </div>
      {data?.last_run && (
        <div className="text-muted-foreground ml-auto flex items-center gap-2">
          <History className="size-4" />
          {t('backendStatus.lastRun')} <RunStatusPill status={data.last_run.status} />
        </div>
      )}
      </div>
    </div>
  )
}
