import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { ConnectionStatus } from '../run-status'
import { EntraGuide } from '../entra-guide'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import { LockableInput } from './lockable-input'
import { GraphResultCard } from '@/pages/setup'
import type { SettingsTabProps } from '@/pages/settings'
import { hasAdminRights, useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { MASK_MARKER } from '@/lib/constants'
import type { DashboardData, GraphTestResult } from '@/lib/types'

export function GraphTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const isAdmin = hasAdminRights(useAuth().user?.role)
  // Steigt nach jedem Speichern — sperrt die geschützten Felder danach wieder.
  const [lockSignal, setLockSignal] = useState(0)
  const [tenant, setTenant] = useState(String(settings['graph.tenant_id'] ?? ''))
  const [clientId, setClientId] = useState(String(settings['graph.client_id'] ?? ''))
  const [secret, setSecret] = useState('')
  const [secretExpires, setSecretExpires] = useState(
    String(settings['graph.client_secret_expires_at'] ?? ''),
  )
  const [group, setGroup] = useState(String(settings['sync.group_id'] ?? ''))
  const [testing, setTesting] = useState(false)
  // Nutzt den bereits gecachten Dashboard-Query — kein zusätzlicher Request.
  const { data: dash } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardData>('/dashboard'),
  })
  const [result, setResult] = useState<GraphTestResult | null>(null)
  const secretSet = settings['graph.client_secret'] === MASK_MARKER

  // Jeder Speichern-Button speichert NUR die Felder seines eigenen Abschnitts.
  const saveGraph = async () => {
    await save({
      'graph.tenant_id': tenant,
      'graph.client_id': clientId,
      ...(secret ? { 'graph.client_secret': secret } : {}),
      'graph.client_secret_expires_at': secretExpires.trim(),
    })
    setSecret('') // getippter Klartext raus, Feld zeigt danach wieder die Maske
    setLockSignal((n) => n + 1)
  }

  const saveGroup = async () => {
    await save({ 'sync.group_id': group.trim() })
    setLockSignal((n) => n + 1)
  }

  const test = async () => {
    setTesting(true)
    setResult(null)
    try {
      await saveGraph()
      const res = await api.post<GraphTestResult>('/settings/graph/test', {})
      setResult(res)
      if (res.connected && res.missing_permissions.length === 0)
        toast.success(t('graphTab.toast.connectionSuccess'))
      else if (res.connected) toast.warning(t('graphTab.toast.connectedMissingPermissions'))
      else toast.error(res.error ?? t('graphTab.toast.connectionFailed'))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-4">
      <Section
        title={t('graphTab.graph.title')}
        description={t('graphTab.graph.description')}
        footer={
          <>
            <Button variant="outline" onClick={test} loading={testing}>
              {t('graphTab.graph.testButton')}
            </Button>
            <Button onClick={saveGraph} loading={saving}>
              {t('graphTab.graph.saveButton')}
            </Button>
          </>
        }
      >
        <ConnectionStatus
          ok={!!dash?.backends.graph_configured}
          label={
            dash?.backends.graph_configured
              ? t('backendStatus.graph.connected')
              : t('backendStatus.graph.notConfigured')
          }
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={t('graphTab.graph.tenantId')} className="sm:col-span-2">
            <LockableInput
              value={tenant}
              onChange={setTenant}
              hasSavedValue={!!settings['graph.tenant_id']}
              lockSignal={lockSignal}
              canUnlock={isAdmin}
            />
          </Field>
          <Field label={t('graphTab.graph.clientId')} className="sm:col-span-2">
            <LockableInput
              value={clientId}
              onChange={setClientId}
              hasSavedValue={!!settings['graph.client_id']}
              lockSignal={lockSignal}
              canUnlock={isAdmin}
            />
          </Field>
          <Field
            label={t('graphTab.graph.clientSecret')}
            hint={secretSet ? t('graphTab.graph.clientSecretHint') : undefined}
            className="sm:col-span-2"
          >
            <LockableInput
              type="password"
              value={secret}
              onChange={setSecret}
              placeholder={secretSet ? '••••••••' : ''}
              hasSavedValue={secretSet}
              lockSignal={lockSignal}
              canUnlock={isAdmin}
            />
          </Field>
          <Field
            label={t('graphTab.graph.secretExpires')}
            hint={t('graphTab.graph.secretExpiresHint')}
            className="sm:col-span-2"
          >
            <Input
              type="date"
              value={secretExpires}
              onChange={(e) => setSecretExpires(e.target.value)}
            />
          </Field>
        </div>
        {result && <GraphResultCard result={result} />}
      </Section>

      <Section
        title={t('graphTab.group.title')}
        description={t('graphTab.group.description')}
        footer={
          <Button onClick={saveGroup} loading={saving}>
            {t('graphTab.group.saveButton')}
          </Button>
        }
      >
        <Field label={t('graphTab.group.objectIdLabel')} hint={t('graphTab.group.objectIdHint')}>
          <LockableInput
            value={group}
            onChange={setGroup}
            placeholder={t('graphTab.group.objectIdPlaceholder')}
            className="font-mono"
            hasSavedValue={!!settings['sync.group_id']}
            lockSignal={lockSignal}
            canUnlock={isAdmin}
          />
        </Field>

        <div className="border-warning/40 bg-warning/10 text-foreground rounded-lg border p-3 text-xs">
          <p>
            <strong>{t('graphTab.group.permWarning.title')}</strong>{' '}
            {t('graphTab.group.permWarning.text1')}{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono">GroupMember.Read.All</code>{' '}
            {t('graphTab.group.permWarning.text2')}
          </p>
        </div>

        <div className="border-border bg-muted/40 text-muted-foreground space-y-3 rounded-lg border p-4 text-xs">
          <p className="text-foreground text-sm font-medium">
            {t('graphTab.group.template.heading')}
          </p>
          <p>
            {t('graphTab.group.template.intro1')}{' '}
            <strong>{t('graphTab.group.template.introGroupsNew')}</strong>{' '}
            {t('graphTab.group.template.intro2')}{' '}
            <strong>{t('graphTab.group.template.introDynamicUser')}</strong>{' '}
            {t('graphTab.group.template.intro3')}{' '}
            <strong>{t('graphTab.group.template.introEditRule')}</strong>{' '}
            {t('graphTab.group.template.intro4')}
          </p>

          <div className="space-y-1.5">
            <p className="text-foreground font-medium">{t('graphTab.group.template.rule1Title')}</p>
            <p>{t('graphTab.group.template.rule1Desc')}</p>
            <pre className="bg-card text-foreground overflow-x-auto rounded-md p-3 font-mono">
              {`(user.accountEnabled -eq true) and\n(user.userType -eq "Member") and\n(user.assignedPlans -any (assignedPlan.capabilityStatus -eq "Enabled"))`}
            </pre>
          </div>

          <div className="space-y-1.5">
            <p className="text-foreground font-medium">{t('graphTab.group.template.rule2Title')}</p>
            <p>
              {t('graphTab.group.template.rule2Desc1')}{' '}
              <code className="bg-card rounded px-1 py-0.5 font-mono">GROSSBUCHSTABEN</code>
              {t('graphTab.group.template.rule2Desc2')}
            </p>
            <pre className="bg-card text-foreground overflow-x-auto rounded-md p-3 font-mono">
              {`(user.accountEnabled -eq true) and\n(user.userType -eq "Member") and\n(user.userPrincipalName -match "@FIRMA-DOMAIN.CH$") and\n(user.department -eq "ABTEILUNG")`}
            </pre>
            <ul className="list-disc space-y-0.5 pl-4">
              <li>
                <code className="bg-card rounded px-1 py-0.5 font-mono">FIRMA-DOMAIN.CH</code>{' '}
                {t('graphTab.group.template.placeholderDomain')}
              </li>
              <li>
                <code className="bg-card rounded px-1 py-0.5 font-mono">ABTEILUNG</code>{' '}
                {t('graphTab.group.template.placeholderDept1')}{' '}
                <code className="font-mono">department</code>{' '}
                {t('graphTab.group.template.placeholderDept2')}
              </li>
            </ul>
          </div>

          <p>
            {t('graphTab.group.template.static1')}{' '}
            <strong>{t('graphTab.group.template.staticGroup')}</strong>
            {t('graphTab.group.template.static2')}
          </p>
        </div>
      </Section>

      <EntraGuide />
    </div>
  )
}
