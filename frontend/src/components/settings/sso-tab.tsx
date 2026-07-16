import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import { LockableInput } from './lockable-input'
import type { SettingsTabProps } from '@/pages/settings'
import { useAuth } from '@/lib/auth'

export function SsoTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const isAdmin = useAuth().user?.role === 'admin'
  const [lockSignal, setLockSignal] = useState(0)
  const [enabled, setEnabled] = useState(Boolean(settings['oidc.enabled'] ?? false))
  const [groupId, setGroupId] = useState(String(settings['oidc.admin_group_id'] ?? ''))
  const [auditorGroupId, setAuditorGroupId] = useState(
    String(settings['oidc.auditor_group_id'] ?? ''),
  )
  const [label, setLabel] = useState(
    String(settings['oidc.button_label'] ?? t('ssoTab.defaultButtonLabel')),
  )
  const [publicUrl, setPublicUrl] = useState(String(settings['app.public_url'] ?? ''))
  const base = publicUrl.trim().replace(/\/+$/, '') || window.location.origin
  const redirectUri = `${base}/api/auth/oidc/callback`

  const onSave = async () => {
    await save({
      'oidc.enabled': enabled,
      'oidc.admin_group_id': groupId,
      'oidc.auditor_group_id': auditorGroupId.trim(),
      'oidc.button_label': label,
      'app.public_url': publicUrl.trim().replace(/\/+$/, ''),
    })
    setLockSignal((n) => n + 1)
  }

  return (
    <Section
      title={t('ssoTab.title')}
      description={t('ssoTab.description')}
      footer={
        <Button onClick={onSave} loading={saving}>
          {t('ssoTab.saveButton')}
        </Button>
      }
    >
      <div className="border-border flex items-center justify-between rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">{t('ssoTab.enable.title')}</p>
          <p className="text-muted-foreground text-xs">{t('ssoTab.enable.description')}</p>
        </div>
        <Switch checked={enabled} onCheckedChange={setEnabled} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label={t('ssoTab.publicUrl.label')}
          hint={t('ssoTab.publicUrl.hint')}
          className="sm:col-span-2"
        >
          <LockableInput
            value={publicUrl}
            onChange={setPublicUrl}
            placeholder="https://domain.example.com"
            hasSavedValue={!!settings['app.public_url']}
            lockSignal={lockSignal}
            canUnlock={isAdmin}
          />
        </Field>
        <Field
          label={t('ssoTab.adminGroup.label')}
          hint={t('ssoTab.adminGroup.hint')}
          className="sm:col-span-2"
        >
          <LockableInput
            value={groupId}
            onChange={setGroupId}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono"
            hasSavedValue={!!settings['oidc.admin_group_id']}
            lockSignal={lockSignal}
            canUnlock={isAdmin}
          />
        </Field>
        <Field
          label={t('ssoTab.auditorGroup.label')}
          hint={t('ssoTab.auditorGroup.hint')}
          className="sm:col-span-2"
        >
          <LockableInput
            value={auditorGroupId}
            onChange={setAuditorGroupId}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono"
            hasSavedValue={!!settings['oidc.auditor_group_id']}
            lockSignal={lockSignal}
            canUnlock={isAdmin}
          />
        </Field>
        <Field label={t('ssoTab.buttonLabel.label')} className="sm:col-span-2">
          <LockableInput
            value={label}
            onChange={setLabel}
            hasSavedValue={!!settings['oidc.button_label']}
            lockSignal={lockSignal}
            canUnlock={isAdmin}
          />
        </Field>
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4 text-xs">
        <p className="mb-2 font-medium">{t('ssoTab.setup.heading')}</p>
        <ol className="text-muted-foreground list-decimal space-y-1 pl-4">
          <li>
            {t('ssoTab.setup.step1a')} <strong>Web</strong> {t('ssoTab.setup.step1b')}{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono break-all">{redirectUri}</code>
          </li>
          <li>
            {t('ssoTab.setup.step2a')} <strong>{t('ssoTab.setup.step2IdToken')}</strong>{' '}
            {t('ssoTab.setup.step2b')}
          </li>
          <li>
            {t('ssoTab.setup.step3a')} <strong>{t('ssoTab.setup.step3Manifest')}</strong>{' '}
            <code className="font-mono">groupMembershipClaims</code> {t('ssoTab.setup.step3b')}{' '}
            <code className="font-mono">"SecurityGroup"</code> {t('ssoTab.setup.step3c')}
          </li>
          <li>
            {t('ssoTab.setup.step4a')} <strong>{t('ssoTab.setup.step4Sync')}</strong>{' '}
            {t('ssoTab.setup.step4b')} <strong>{t('ssoTab.setup.step4ApiPerms')}</strong>{' '}
            {t('ssoTab.setup.step4c')} <strong>{t('ssoTab.setup.step4AppPerm')}</strong>{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono">GroupMember.Read.All</code>{' '}
            {t('ssoTab.setup.step4d')} <strong>{t('ssoTab.setup.step4Consent')}</strong>
            {t('ssoTab.setup.step4e')}
          </li>
        </ol>
        <p className="text-muted-foreground mt-3 text-xs">
          {t('ssoTab.setup.photoNote1')}{' '}
          <code className="bg-card rounded px-1 py-0.5 font-mono">User.Read.All</code>{' '}
          {t('ssoTab.setup.photoNote2')}
        </p>
      </div>
    </Section>
  )
}
