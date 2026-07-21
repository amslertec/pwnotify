import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { ChipInput, Field, Panel, Section, ToggleRow } from './section'
import type { SettingsTabProps } from '@/pages/settings'

export function AlertsTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const [enabled, setEnabled] = useState(Boolean(settings['alerts.enabled'] ?? false))
  const [recipients, setRecipients] = useState<string[]>(
    (settings['alerts.recipients'] as string[]) ?? [],
  )
  const [recipientInput, setRecipientInput] = useState('')
  const [digest, setDigest] = useState(Boolean(settings['alerts.digest'] ?? true))
  const [onFailure, setOnFailure] = useState(Boolean(settings['alerts.on_failure'] ?? true))

  const addRecipient = () => {
    const r = recipientInput.trim().toLowerCase()
    if (r && !recipients.includes(r)) setRecipients([...recipients, r])
    setRecipientInput('')
  }

  const onSave = () =>
    save({
      'alerts.enabled': enabled,
      'alerts.recipients': recipients,
      'alerts.digest': digest,
      'alerts.on_failure': onFailure,
    })

  return (
    <div className="space-y-4">
      <Section
        title={t('alertsTab.title')}
        description={t('alertsTab.description')}
        footer={
          <Button onClick={onSave} loading={saving}>
            {t('alertsTab.save')}
          </Button>
        }
      >
        <Panel>
          <ToggleRow
            title={t('alertsTab.enabled.title')}
            description={t('alertsTab.enabled.description')}
            checked={enabled}
            onCheckedChange={setEnabled}
          />
        </Panel>

        <Field label={t('alertsTab.recipients.label')} hint={t('alertsTab.recipients.hint')}>
          <ChipInput
            values={recipients}
            chipLabel={(r) => r}
            onRemove={(r) => setRecipients(recipients.filter((x) => x !== r))}
            input={recipientInput}
            onInputChange={setRecipientInput}
            onAdd={addRecipient}
            placeholder={t('alertsTab.recipients.placeholder')}
            removeLabel={t('alertsTab.remove')}
            mono
            type="email"
            inputClassName="w-56"
          />
        </Field>

        <Panel>
          <ToggleRow
            title={t('alertsTab.digest.title')}
            description={t('alertsTab.digest.description')}
            checked={digest}
            onCheckedChange={setDigest}
          />
          <ToggleRow
            title={t('alertsTab.onFailure.title')}
            description={t('alertsTab.onFailure.description')}
            checked={onFailure}
            onCheckedChange={setOnFailure}
          />
        </Panel>
      </Section>
    </div>
  )
}
