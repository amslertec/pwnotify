import { useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { ChipInput, Field, Panel, Section, ToggleRow } from './section'
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
    Boolean(settings['sync.shared_detect_unlicensed'] ?? false),
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
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Section
        title={t('policyTab.passwordPolicy.title')}
        description={t('policyTab.passwordPolicy.description')}
        footer={
          <Button onClick={onSave} loading={saving}>
            {t('policyTab.save')}
          </Button>
        }
      >
        <Panel>
          <ToggleRow
            title={t('policyTab.autoDetect.title')}
            description={
              <Trans
                i18nKey="policyTab.autoDetect.description"
                components={{ code: <code className="font-mono" /> }}
              />
            }
            checked={auto}
            onCheckedChange={setAuto}
          />
        </Panel>

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
        <Panel>
          <ToggleRow
            title={t('policyTab.detectUnlicensed.title')}
            description={t('policyTab.detectUnlicensed.description')}
            checked={detectUnlicensed}
            onCheckedChange={setDetectUnlicensed}
          />
        </Panel>

        <Field label={t('policyTab.patterns.label')} hint={t('policyTab.patterns.hint')}>
          <ChipInput
            values={patterns}
            chipLabel={(p) => p}
            onRemove={(p) => setPatterns(patterns.filter((x) => x !== p))}
            input={patternInput}
            onInputChange={setPatternInput}
            onAdd={addPattern}
            placeholder={t('policyTab.patterns.placeholder')}
            removeLabel={t('policyTab.remove')}
            mono
            inputClassName="w-40"
          />
        </Field>
      </Section>
    </div>
  )
}
