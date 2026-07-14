import { X } from 'lucide-react'
import { useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'

export function PolicyTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
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
        title={t('policyTab.passwordPolicy.title')}
        description={t('policyTab.passwordPolicy.description')}
        footer={
          <Button onClick={onSave} loading={saving}>
            {t('policyTab.save')}
          </Button>
        }
      >
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('policyTab.autoDetect.title')}</p>
            <p className="text-muted-foreground text-xs">
              <Trans
                i18nKey="policyTab.autoDetect.description"
                components={{ code: <code className="font-mono" /> }}
              />
            </p>
          </div>
          <Switch checked={auto} onCheckedChange={setAuto} />
        </div>

        <Field
          label={t('policyTab.manualValidity.label')}
          hint={t('policyTab.manualValidity.hint')}
        >
          <Input
            type="number"
            value={override}
            onChange={(e) => setOverride(e.target.value)}
            placeholder={t('policyTab.manualValidity.placeholder')}
            className="max-w-40"
          />
        </Field>
      </Section>

      <Section
        title={t('policyTab.shared.title')}
        description={t('policyTab.shared.description')}
        footer={
          <Button onClick={onSave} loading={saving}>
            {t('policyTab.save')}
          </Button>
        }
      >
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('policyTab.detectUnlicensed.title')}</p>
            <p className="text-muted-foreground text-xs">
              {t('policyTab.detectUnlicensed.description')}
            </p>
          </div>
          <Switch checked={detectUnlicensed} onCheckedChange={setDetectUnlicensed} />
        </div>

        <Field label={t('policyTab.patterns.label')} hint={t('policyTab.patterns.hint')}>
          <div className="flex flex-wrap items-center gap-2">
            {patterns.map((p) => (
              <span
                key={p}
                className="bg-muted inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-mono text-sm"
              >
                {p}
                <button
                  onClick={() => setPatterns(patterns.filter((x) => x !== p))}
                  aria-label={t('policyTab.remove')}
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
            <Input
              value={patternInput}
              onChange={(e) => setPatternInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addPattern())}
              placeholder={t('policyTab.patterns.placeholder')}
              className="w-40 font-mono"
            />
          </div>
        </Field>
      </Section>
    </div>
  )
}
