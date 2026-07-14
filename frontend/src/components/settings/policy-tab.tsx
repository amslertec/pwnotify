import { X } from 'lucide-react'
import { useState } from 'react'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'

export function PolicyTab({ settings, save, saving }: SettingsTabProps) {
  const [auto, setAuto] = useState(Boolean(settings['policy.auto_detect'] ?? true))
  const [override, setOverride] = useState(
    settings['policy.validity_days_override'] == null
      ? ''
      : String(settings['policy.validity_days_override']),
  )
  const [patterns, setPatterns] = useState<string[]>(
    (settings['sync.shared_patterns'] as string[]) ?? [],
  )
  const [patternInput, setPatternInput] = useState('')
  const [detectUnlicensed, setDetectUnlicensed] = useState(
    Boolean(settings['sync.shared_detect_unlicensed'] ?? true),
  )

  const addPattern = () => {
    const p = patternInput.trim().toLowerCase()
    if (p && !patterns.includes(p)) setPatterns([...patterns, p])
    setPatternInput('')
  }

  const onSave = () =>
    save({
      'policy.auto_detect': auto,
      'policy.validity_days_override': override === '' ? null : Number(override),
      'sync.shared_patterns': patterns,
      'sync.shared_detect_unlicensed': detectUnlicensed,
    })

  return (
    <div className="space-y-4">
      <Section
        title="Passwort-Policy"
        description="Gültigkeitsdauer aus der Domain-Konfiguration erkennen oder manuell setzen."
        footer={
          <Button onClick={onSave} loading={saving}>
            Speichern
          </Button>
        }
      >
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">Automatische Erkennung</p>
            <p className="text-muted-foreground text-xs">
              Liest <code className="font-mono">passwordValidityPeriodInDays</code> aus den
              Entra-Domains.
            </p>
          </div>
          <Switch checked={auto} onCheckedChange={setAuto} />
        </div>

        <Field
          label="Manuelle Gültigkeitsdauer (Tage)"
          hint="Setzen, wenn Passwörter im Tenant nie ablaufen — sonst gibt es kein Ablaufdatum. Überschreibt die Auto-Erkennung."
        >
          <Input
            type="number"
            value={override}
            onChange={(e) => setOverride(e.target.value)}
            placeholder="z. B. 90"
            className="max-w-40"
          />
        </Field>
      </Section>

      <Section
        title="Shared Mailboxes"
        description="Erkannte Shared/Room/Equipment-Postfächer werden aus der Benutzerliste ausgeblendet (eigene Ansicht in der Status-Auswahl) und nicht benachrichtigt. Greift beim nächsten Sync."
        footer={
          <Button onClick={onSave} loading={saving}>
            Speichern
          </Button>
        }
      >
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">Automatisch erkennen (Postfach ohne Lizenz)</p>
            <p className="text-muted-foreground text-xs">
              Konto mit Postfach, aber ohne zugewiesene Lizenz → Shared Mailbox. Zuverlässig, da
              normale Benutzer für ein Postfach eine Lizenz brauchen.
            </p>
          </div>
          <Switch checked={detectUnlicensed} onCheckedChange={setDetectUnlicensed} />
        </div>

        <Field
          label="Zusätzliche Muster (optional, Glob z. B. noreply@*)"
          hint="Manueller Override — greift zusätzlich zur automatischen Erkennung."
        >
          <div className="flex flex-wrap items-center gap-2">
            {patterns.map((p) => (
              <span
                key={p}
                className="bg-muted inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-mono text-sm"
              >
                {p}
                <button
                  onClick={() => setPatterns(patterns.filter((x) => x !== p))}
                  aria-label="Entfernen"
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
            <Input
              value={patternInput}
              onChange={(e) => setPatternInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addPattern())}
              placeholder="+ Muster"
              className="w-40 font-mono"
            />
          </div>
        </Field>
      </Section>
    </div>
  )
}
