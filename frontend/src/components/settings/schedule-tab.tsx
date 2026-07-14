import { useEffect, useState } from 'react'
import { X } from 'lucide-react'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/format'

export function ScheduleTab({ settings, save, saving }: SettingsTabProps) {
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
      title="Zeitplan"
      description="Wann PwNotify prüft und wie gestaffelt erinnert wird."
      footer={
        <Button onClick={onSave} loading={saving}>
          Speichern
        </Button>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Cron-Ausdruck" hint="5 Felder: Minute Stunde Tag Monat Wochentag">
          <Input value={cron} onChange={(e) => setCron(e.target.value)} className="font-mono" />
        </Field>
        <Field label="Zeitzone">
          <Input value={tz} onChange={(e) => setTz(e.target.value)} />
        </Field>
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4">
        <p className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          Nächste 5 Ausführungen
        </p>
        {previewErr ? (
          <p className="text-danger text-sm">Ungültiger Cron-Ausdruck</p>
        ) : (
          <ul className="space-y-1 font-mono text-sm">
            {preview.map((r) => (
              <li key={r}>{fmtDateTime(r)}</li>
            ))}
          </ul>
        )}
      </div>

      <Field label="Reminder-Stufen (Tage vor Ablauf)">
        <div className="flex flex-wrap items-center gap-2">
          {days.map((d) => (
            <span
              key={d}
              className="bg-primary/10 text-primary inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-sm font-medium"
            >
              {d} T
              <button onClick={() => setDays(days.filter((x) => x !== d))} aria-label="Entfernen">
                <X className="size-3" />
              </button>
            </span>
          ))}
          <Input
            value={dayInput}
            onChange={(e) => setDayInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addDay())}
            placeholder="+ Tage"
            className="w-20"
          />
        </div>
      </Field>

      <div className="border-border flex items-center justify-between rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">Probelauf-Modus (Dry-Run)</p>
          <p className="text-muted-foreground text-xs">
            Alles berechnen und protokollieren, aber keine Mails versenden.
          </p>
        </div>
        <Switch checked={dryRun} onCheckedChange={setDryRun} />
      </div>
    </Section>
  )
}
