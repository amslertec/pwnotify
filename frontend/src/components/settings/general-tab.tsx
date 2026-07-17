import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpCircle, CheckCircle2, ExternalLink, RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import { isDefaultContext, useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import type { InstanceSettings, VersionInfo } from '@/lib/types'

export function GeneralTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const [updateCheck, setUpdateCheck] = useState(Boolean(settings['app.update_check'] ?? true))
  const [require2fa, setRequire2fa] = useState(Boolean(settings['auth.require_2fa'] ?? false))
  // Aufbewahrungsfristen in Tagen; 0 = unbegrenzt (Standard).
  const [auditDays, setAuditDays] = useState(String(settings['audit.retention_days'] ?? 0))
  const [userDays, setUserDays] = useState(String(settings['privacy.user_retention_days'] ?? 0))
  const [logDays, setLogDays] = useState(String(settings['privacy.log_retention_days'] ?? 0))
  const qc = useQueryClient()
  const { data: ver } = useQuery({
    queryKey: ['version'],
    queryFn: () => api.get<VersionInfo>('/version'),
    staleTime: 60 * 60 * 1000,
  })

  const check = useMutation({
    mutationFn: () => api.get<VersionInfo>('/version?force=true'),
    onSuccess: (data) => {
      qc.setQueryData(['version'], data)
      if (!data.enabled) toast.info(t('generalTab.toast.checkDisabled'))
      else if (data.update_available)
        toast.info(t('generalTab.toast.updateAvailable', { version: data.latest }))
      else toast.success(t('generalTab.toast.upToDate'))
    },
    onError: () => toast.error(t('generalTab.toast.checkFailed')),
  })

  return (
    <div className="space-y-4">
      <Section
        title={t('generalTab.version.title')}
        description={t('generalTab.version.description')}
        footer={
          <Button onClick={() => save({ 'app.update_check': updateCheck })} loading={saving}>
            {t('generalTab.version.saveButton')}
          </Button>
        }
      >
        <div className="border-border flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
          <div className="flex items-center gap-3">
            {ver?.update_available ? (
              <ArrowUpCircle className="text-primary size-5 shrink-0" />
            ) : (
              <CheckCircle2 className="text-success size-5 shrink-0" />
            )}
            <div>
              <p className="text-sm font-medium">
                {t('generalTab.version.installed', { version: ver?.current ?? '—' })}
                {ver?.update_available && ver.latest
                  ? t('generalTab.version.newAvailableSuffix', { version: ver.latest })
                  : ''}
              </p>
              <p className="text-muted-foreground text-xs">
                {ver && !ver.enabled
                  ? t('generalTab.toast.checkDisabled')
                  : ver?.update_available
                    ? t('generalTab.version.newerReleaseAvailable')
                    : t('generalTab.toast.upToDate')}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {ver?.update_available && (
              <a
                href={ver.release_url}
                target="_blank"
                rel="noreferrer"
                className="text-primary inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2"
              >
                {t('generalTab.version.viewRelease')} <ExternalLink className="size-3.5" />
              </a>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => check.mutate()}
              loading={check.isPending}
            >
              <RefreshCw className="size-4" /> {t('generalTab.version.checkNow')}
            </Button>
          </div>
        </div>

        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div className="pr-3">
            <p className="text-sm font-medium">{t('generalTab.autoCheck.title')}</p>
            <p className="text-muted-foreground text-xs">{t('generalTab.autoCheck.description')}</p>
          </div>
          <Switch checked={updateCheck} onCheckedChange={setUpdateCheck} />
        </div>
      </Section>
      <Section
        title={t('generalTab.security.title')}
        description={t('generalTab.security.description')}
        footer={
          <Button onClick={() => save({ 'auth.require_2fa': require2fa })} loading={saving}>
            {t('generalTab.security.saveButton')}
          </Button>
        }
      >
        <label className="border-border flex items-start justify-between gap-4 rounded-lg border p-4">
          <span>
            <span className="block text-sm font-medium">{t('generalTab.security.require2fa')}</span>
            <span className="text-muted-foreground mt-1 block text-sm">
              {t('generalTab.security.require2faHint')}
            </span>
          </span>
          <Switch checked={require2fa} onCheckedChange={setRequire2fa} />
        </label>
      </Section>

      <Section
        title={t('generalTab.retention.title')}
        description={t('generalTab.retention.description')}
        footer={
          <Button
            onClick={() =>
              save({
                'audit.retention_days': Number(auditDays) || 0,
                'privacy.user_retention_days': Number(userDays) || 0,
                'privacy.log_retention_days': Number(logDays) || 0,
              })
            }
            loading={saving}
          >
            {t('generalTab.retention.saveButton')}
          </Button>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label={t('generalTab.retention.userDays')}
            hint={t('generalTab.retention.userDaysHint')}
          >
            <Input
              type="number"
              min={0}
              value={userDays}
              onChange={(e) => setUserDays(e.target.value)}
            />
          </Field>
          <Field
            label={t('generalTab.retention.logDays')}
            hint={t('generalTab.retention.logDaysHint')}
          >
            <Input
              type="number"
              min={0}
              value={logDays}
              onChange={(e) => setLogDays(e.target.value)}
            />
          </Field>
          <Field
            label={t('generalTab.retention.auditDays')}
            hint={t('generalTab.retention.auditDaysHint')}
            className="sm:col-span-2"
          >
            <Input
              type="number"
              min={0}
              value={auditDays}
              onChange={(e) => setAuditDays(e.target.value)}
            />
          </Field>
        </div>
      </Section>

      <MultiTenantSection />
    </div>
  )
}

/** Instanzweiter Schalter für die Mandantenfähigkeit (Access-Modell/Superadmin-Phase,
 *  Task 7) — nur für Superadmins sichtbar. Läuft bewusst NICHT über die
 *  Kunden-skalierte `settings`/`save`-Props dieser Komponente, sondern direkt gegen
 *  `GET/PUT /admin/instance` (instanzweit, nicht pro Kunde). Ist der Schalter aus,
 *  bleibt PwNotify für alle optisch/funktional identisch mit dem Einzel-Kunden-Stand
 *  (kein Kunden-Umschalter, keine Kunden-Navigation — siehe `sidebar.tsx` /
 *  `tenant-switcher.tsx`). */
function MultiTenantSection() {
  const { t } = useTranslation()
  const { user: me, refresh } = useAuth()
  const qc = useQueryClient()
  const [defaultName, setDefaultName] = useState('')

  const { data: instance, isLoading } = useQuery({
    queryKey: ['admin-instance'],
    queryFn: () => api.get<InstanceSettings>('/admin/instance'),
    enabled: isDefaultContext(me),
  })

  // Einmaliger Seed aus dem Server-Stand -- danach steuert nur noch die lokale Eingabe
  // (kein Re-Sync bei Refetch, sonst gingen ungespeicherte Tastatureingaben verloren).
  useEffect(() => {
    if (instance) setDefaultName(instance.default_tenant_name)
  }, [instance])

  const toggleMode = useMutation({
    mutationFn: (value: boolean) =>
      api.put<InstanceSettings>('/admin/instance', { multi_tenant_mode: value }),
    onSuccess: (data) => {
      qc.setQueryData(['admin-instance'], data)
      void qc.invalidateQueries({ queryKey: ['admin-instance'] })
      // Der Schalter steuert Sidebar/Switcher über `user.multi_tenant_mode` -- ohne
      // Refresh bliebe die Oberfläche bis zum nächsten Login/Reload veraltet.
      void refresh()
      toast.success(t('generalTab.multiTenant.modeSaved'))
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const renameDefault = useMutation({
    mutationFn: () =>
      api.put<InstanceSettings>('/admin/instance', { default_tenant_name: defaultName.trim() }),
    onSuccess: (data) => {
      qc.setQueryData(['admin-instance'], data)
      void qc.invalidateQueries({ queryKey: ['admin-instance'] })
      // Der aktive Kunde des Aufrufers kann der umbenannte Standard-Kunde sein --
      // Switcher/Sidebar zeigen das nur nach einem Refresh sofort.
      void refresh()
      void qc.invalidateQueries({ queryKey: ['admin-tenants'] })
      toast.success(t('generalTab.multiTenant.defaultNameSaved'))
    },
    onError: (e) => toast.error(translateError(e)),
  })

  // Belt-and-braces: die Tab-Ausblendung in settings.tsx deckt das schon ab, aber diese
  // Komponente soll auch stand-alone (z. B. bei künftiger Wiederverwendung) korrekt gaten.
  if (!isDefaultContext(me)) return null

  return (
    <Section
      title={t('generalTab.multiTenant.title')}
      description={t('generalTab.multiTenant.description')}
    >
      <label className="border-border flex items-start justify-between gap-4 rounded-lg border p-4">
        <span>
          <span className="block text-sm font-medium">{t('generalTab.multiTenant.modeLabel')}</span>
          <span className="text-muted-foreground mt-1 block text-sm">
            {t('generalTab.multiTenant.modeHint')}
          </span>
        </span>
        <Switch
          checked={Boolean(instance?.multi_tenant_mode)}
          disabled={isLoading || toggleMode.isPending}
          onCheckedChange={(v) => toggleMode.mutate(v)}
        />
      </label>

      <Field
        label={t('generalTab.multiTenant.defaultNameLabel')}
        hint={t('generalTab.multiTenant.defaultNameHint')}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={defaultName}
            onChange={(e) => setDefaultName(e.target.value)}
            disabled={isLoading}
            className="max-w-sm"
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => renameDefault.mutate()}
            loading={renameDefault.isPending}
            disabled={isLoading || defaultName.trim().length < 1}
          >
            {t('generalTab.multiTenant.defaultNameSave')}
          </Button>
        </div>
      </Field>
    </Section>
  )
}
