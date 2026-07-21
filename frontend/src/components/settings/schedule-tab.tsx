import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { RunTriggerButtons } from '../run-status'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { ChipInput, Field, Panel, Section, ToggleRow } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { hasAdminRights, useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/format'

export function ScheduleTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const isAdmin = hasAdminRights(useAuth().user?.role)
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

      <div className="border-border bg-muted/40 rounded-lg border p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
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
        <ChipInput
          values={days}
          chipLabel={(d) => t('scheduleTab.dayChip', { n: d })}
          onRemove={(d) => setDays(days.filter((x) => x !== d))}
          input={dayInput}
          onInputChange={setDayInput}
          onAdd={addDay}
          placeholder={t('scheduleTab.daysPlaceholder')}
          removeLabel={t('scheduleTab.remove')}
          tone="primary"
          inputClassName="w-20"
        />
      </Field>

      <Panel>
        <ToggleRow
          title={t('scheduleTab.dryRun.title')}
          description={t('scheduleTab.dryRun.description')}
          checked={dryRun}
          onCheckedChange={setDryRun}
        />
      </Panel>
    </Section>
  )
}
