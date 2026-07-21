import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '../ui/button'
import { Callout, Field, Panel, Section, ToggleRow } from './section'
import { LockableInput } from './lockable-input'
import type { SettingsTabProps } from '@/pages/settings'
import { hasAdminRights, useAuth } from '@/lib/auth'

/** Ob die Rollen-Gruppen-Felder (Admin-/Auditor-Gruppen-Objekt-ID) angezeigt werden. In
 *  Multi-Tenant-Modus werden Rollen pro Team auf der Kunden-Seite verwaltet -- diese
 *  instanzweiten OIDC-Gruppen-Felder wären dort irreführend (Task 6). Reine Funktion, damit
 *  sie ohne Rendering testbar ist (Muster wie `isSwitcherVisible`). */
export function showRoleGroupFields(multiTenant: boolean): boolean {
  return !multiTenant
}

/** Baut den Save-Payload für die SSO-Settings. Im Multi-Tenant-Modus dürfen die beiden
 *  Gruppen-Keys NICHT mitgeschickt werden -- sonst würde ein Speichern anderer SSO-Werte
 *  (z. B. Public-URL) die Kunden-relevanten Gruppen-IDs auf dem Server leeren, obwohl die
 *  Felder in dieser Ansicht gar nicht sichtbar/editierbar waren. Reine Funktion, damit der
 *  wichtige Korrektheitspunkt (welche Keys landen im Payload) ohne DOM testbar ist. */
export function buildSsoSavePayload(
  values: {
    enabled: boolean
    groupId: string
    auditorGroupId: string
    label: string
    publicUrl: string
  },
  multiTenant: boolean,
): Record<string, unknown> {
  return {
    'oidc.enabled': values.enabled,
    ...(showRoleGroupFields(multiTenant)
      ? {
          'oidc.admin_group_id': values.groupId,
          'oidc.auditor_group_id': values.auditorGroupId.trim(),
        }
      : {}),
    'oidc.button_label': values.label,
    'app.public_url': values.publicUrl.trim().replace(/\/+$/, ''),
  }
}

export function SsoTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const { user } = useAuth()
  const isAdmin = hasAdminRights(user?.role)
  const multiTenant = user?.multi_tenant_mode ?? false
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
    await save(
      buildSsoSavePayload({ enabled, groupId, auditorGroupId, label, publicUrl }, multiTenant),
    )
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
      <Panel>
        <ToggleRow
          title={t('ssoTab.enable.title')}
          description={t('ssoTab.enable.description')}
          checked={enabled}
          onCheckedChange={setEnabled}
        />
      </Panel>

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
        {showRoleGroupFields(multiTenant) ? (
          <>
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
          </>
        ) : (
          <p className="text-muted-foreground text-xs sm:col-span-2">
            {t('ssoTab.roleGroupsManagedByTeams')}
          </p>
        )}
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

      <Callout>
        <p className="text-foreground font-medium">{t('ssoTab.setup.heading')}</p>
        <ol className="list-decimal space-y-1 pl-4">
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
        <p>
          {t('ssoTab.setup.photoNote1')}{' '}
          <code className="bg-card rounded px-1 py-0.5 font-mono">User.Read.All</code>{' '}
          {t('ssoTab.setup.photoNote2')}
        </p>
      </Callout>
    </Section>
  )
}
