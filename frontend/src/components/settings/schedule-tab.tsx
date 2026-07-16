import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'

import { RunTriggerButtons } from '../run-status'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/format'

export function ScheduleTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const isAdmin = useAuth().user?.role === 'admin'
  const [cron, setCron] = useState(String(settings['schedule.cron'] ?? '0 8 * * *'))
  const [tz, setTz] = useState(String(settings['schedule.timezone'] ?? 'Europe/Zurich'))
  const [dryRun, setDryRun] = useState(Boolean(settings['schedule.dry_run']))
  const [days, setDays] = useState<number[]>(
    (settings['schedule.reminder_days'] as number[]) ?? [14, 7, 3, 1, 0],
  )
  const [dayInput, setDayInput] = useState('')
  const [preview, setPreview] = useState<string[]>([])
  const [previewErr, setPreviewErr] = useState<string | null>(null)

  useEffect(() => {
    const t = setTimeout(async () => {
      const res = await api.post<{ valid: boolean; next_runs: string[]; error: string | null }>(
        '/settings/schedule/preview',
        { cron, timezone: tz },
      )
      setPreview(res.valid ? res.next_runs : [])
      setPreviewErr(res.valid ? null : res.error)
    }, 300)
    return () => clearTimeout(t)
  }, [cron, tz])

  const addDay = () => {
    const n = parseInt(dayInput, 10)
    if (!Number.isNaN(n) && !days.includes(n)) setDays([...days, n].sort((a, b) => b - a))
    setDayInput('')
  }

  const onSave = () =>
    save({
      'schedule.cron': cron,
      'schedule.timezone': tz,
      'schedule.dry_run': dryRun,
      'schedule.reminder_days': days,
    })

  return (
    <Section
      title={t('scheduleTab.title')}
      description={t('scheduleTab.description')}
      footer={
        <Button onClick={onSave} loading={saving}>
          {t('scheduleTab.save')}
        </Button>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t('scheduleTab.cron.label')} hint={t('scheduleTab.cron.hint')}>
          <Input value={cron} onChange={(e) => setCron(e.target.value)} className="font-mono" />
        </Field>
        <Field label={t('scheduleTab.timezone')}>
          <Input value={tz} onChange={(e) => setTz(e.target.value)} />
        </Field>
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
            {t('scheduleTab.nextRuns')}
          </p>
          {/* Hier sieht man den Zeitplan — also der richtige Ort, ihn zu testen oder
              sofort auszulösen. Nur für Admins. */}
          {isAdmin && <RunTriggerButtons size="sm" />}
        </div>
        {previewErr ? (
          <p className="text-danger text-sm">{t('scheduleTab.invalidCron')}</p>
        ) : (
          <ul className="space-y-1 font-mono text-sm">
            {preview.map((r) => (
              <li key={r}>{fmtDateTime(r)}</li>
            ))}
          </ul>
        )}
      </div>

      <Field label={t('scheduleTab.reminderLevels')}>
        <div className="flex flex-wrap items-center gap-2">
          {days.map((d) => (
            <span
              key={d}
              className="bg-primary/10 text-primary inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-sm font-medium"
            >
              {t('scheduleTab.dayChip', { n: d })}
              <button
                onClick={() => setDays(days.filter((x) => x !== d))}
                aria-label={t('scheduleTab.remove')}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          <Input
            value={dayInput}
            onChange={(e) => setDayInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addDay())}
            placeholder={t('scheduleTab.daysPlaceholder')}
            className="w-20"
          />
        </div>
      </Field>

      <div className="border-border flex items-center justify-between rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">{t('scheduleTab.dryRun.title')}</p>
          <p className="text-muted-foreground text-xs">{t('scheduleTab.dryRun.description')}</p>
        </div>
        <Switch checked={dryRun} onCheckedChange={setDryRun} />
      </div>
    </Section>
  )
}
