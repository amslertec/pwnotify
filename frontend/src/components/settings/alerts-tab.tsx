import { X } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
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
        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('alertsTab.enabled.title')}</p>
            <p className="text-muted-foreground text-xs">{t('alertsTab.enabled.description')}</p>
          </div>
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </div>

        <Field label={t('alertsTab.recipients.label')} hint={t('alertsTab.recipients.hint')}>
          <div className="flex flex-wrap items-center gap-2">
            {recipients.map((r) => (
              <span
                key={r}
                className="bg-muted inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-mono text-sm"
              >
                {r}
                <button
                  onClick={() => setRecipients(recipients.filter((x) => x !== r))}
                  aria-label={t('alertsTab.remove')}
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
            <Input
              type="email"
              value={recipientInput}
              onChange={(e) => setRecipientInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addRecipient())}
              placeholder={t('alertsTab.recipients.placeholder')}
              className="w-56 font-mono"
            />
          </div>
        </Field>

        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('alertsTab.digest.title')}</p>
            <p className="text-muted-foreground text-xs">{t('alertsTab.digest.description')}</p>
          </div>
          <Switch checked={digest} onCheckedChange={setDigest} />
        </div>

        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div>
            <p className="text-sm font-medium">{t('alertsTab.onFailure.title')}</p>
            <p className="text-muted-foreground text-xs">{t('alertsTab.onFailure.description')}</p>
          </div>
          <Switch checked={onFailure} onCheckedChange={setOnFailure} />
        </div>
      </Section>
    </div>
  )
}
