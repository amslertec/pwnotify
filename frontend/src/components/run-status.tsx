import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from './ui/button'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import type { RunDetail } from '@/lib/types'

const STATUS_COLOR: Record<string, string> = {
  success: 'var(--status-ok)',
  partial: 'var(--status-warn)',
  error: 'var(--status-expired)',
  running: 'var(--color-primary)',
}

/** Farbiger Punkt + Wort — ein Element statt Pill und Textdopplung. */
export function RunStatusPill({ status }: { status: string }) {
  const { t } = useTranslation()
  const color = STATUS_COLOR[status] ?? 'var(--status-never)'
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {t(`backendStatus.runStatus.${status}`, { defaultValue: status })}
    </span>
  )
}

/** „Probelauf" + „Jetzt ausführen". Löst einen Lauf aus und aktualisiert die Ansichten.
 *  Gehört an den Ort, an dem der Lauf konfiguriert wird (Zeitplan-Tab). */
export function RunTriggerButtons({ size }: { size?: 'sm' | 'default' }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const trigger = useMutation({
    mutationFn: (dryRun: boolean) => api.post<RunDetail>('/runs/trigger', { dry_run: dryRun }),
    onSuccess: (run) => {
      toast.success(t('runs.toast.completed', { count: run.sent }))
      // Die Läufe-Historie und der Dashboard-Status (letzter Lauf) ändern sich.
      void qc.invalidateQueries({ queryKey: ['runs'] })
      void qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <div className="flex flex-wrap gap-2">
      <Button
        variant="outline"
        size={size}
        onClick={() => trigger.mutate(true)}
        loading={trigger.isPending}
      >
        {t('runs.actions.dryRun')}
      </Button>
      <Button size={size} onClick={() => trigger.mutate(false)} loading={trigger.isPending}>
        <Play /> {t('runs.actions.runNow')}
      </Button>
    </div>
  )
}

/** Schmale Statuszeile über den Feldern eines Settings-Tabs (Graph/Mail). */
export function ConnectionStatus({ ok, label }: { ok: boolean; label: string }) {
  const color = ok ? 'var(--status-ok)' : 'var(--status-never)'
  return (
    <div
      className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
      style={{
        borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
        background: `color-mix(in srgb, ${color} 8%, transparent)`,
      }}
    >
      <span className="size-2 rounded-full" style={{ background: color }} />
      <span className={ok ? 'font-medium' : 'text-muted-foreground'}>{label}</span>
    </div>
  )
}
