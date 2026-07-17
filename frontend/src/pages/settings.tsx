import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { PageHeader } from '@/components/page-header'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api } from '@/lib/api'
import { hasAdminRights, isDefaultContext, useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import type { Settings, User } from '@/lib/types'
import { AlertsTab } from '@/components/settings/alerts-tab'
import { BrandingTab } from '@/components/settings/branding-tab'
import { GeneralTab } from '@/components/settings/general-tab'
import { GraphTab } from '@/components/settings/graph-tab'
import { MailTab } from '@/components/settings/mail-tab'
import { PolicyTab } from '@/components/settings/policy-tab'
import { ScheduleTab } from '@/components/settings/schedule-tab'
import { SsoTab } from '@/components/settings/sso-tab'
import { TemplateTab } from '@/components/settings/template-tab'

export interface SettingsTabProps {
  settings: Settings
  save: (values: Record<string, unknown>) => Promise<void>
  saving: boolean
}

const TABS = [
  { value: 'graph', labelKey: 'settingsPage.tabs.graph' },
  { value: 'sso', labelKey: 'settingsPage.tabs.sso' },
  { value: 'mail', labelKey: 'settingsPage.tabs.mail' },
  { value: 'schedule', labelKey: 'settingsPage.tabs.schedule' },
  { value: 'policy', labelKey: 'settingsPage.tabs.policy' },
  { value: 'alerts', labelKey: 'settingsPage.tabs.alerts' },
  { value: 'branding', labelKey: 'settingsPage.tabs.branding' },
  { value: 'template', labelKey: 'settingsPage.tabs.template' },
  { value: 'general', labelKey: 'settingsPage.tabs.general' },
]

/** General ist die Instanz-Einstellungs-Oberfläche und laut Matrix-B (Design §4) auf
 *  Superadmin + Default-Kontext beschränkt — Admins bekommen sie ausdrücklich NICHT,
 *  und ein Superadmin in einem Kunden-Kontext (nach Umschalten) auch nicht. */
export function showGeneralTab(me: User | null | undefined): boolean {
  return isDefaultContext(me)
}

/** Fällt auf den Default-Tab zurück, wenn ein `?tab=general`-Deep-Link ohne Rechte
 *  aufgerufen wird -- sonst strandet der Aufrufer auf einem ausgeblendeten Tab. */
export function resolveTab(requestedTab: string, showGeneral: boolean): string {
  return requestedTab === 'general' && !showGeneral ? 'graph' : requestedTab
}

export default function SettingsPage() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const showGeneral = showGeneralTab(user)
  const tab = resolveTab(params.get('tab') ?? 'graph', showGeneral)
  const visibleTabs = TABS.filter((item) => item.value !== 'general' || showGeneral)

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<Settings>('/settings'),
  })

  const mutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.put<Settings>('/settings', { values }),
    onSuccess: (data) => {
      qc.setQueryData(['settings'], data)
      void qc.invalidateQueries({ queryKey: ['branding'] })
      toast.success(t('settingsPage.toast.saved'))
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const save = async (values: Record<string, unknown>) => {
    await mutation.mutateAsync(values)
  }

  const tabProps: SettingsTabProps = {
    settings: settings ?? {},
    save,
    saving: mutation.isPending,
  }

  return (
    <div>
      <PageHeader
        title={t('settingsPage.header.title')}
        description={t('settingsPage.header.description')}
      />
      {!hasAdminRights(user?.role) && (
        <div className="border-border bg-muted/40 text-muted-foreground mb-4 rounded-lg border px-4 py-3 text-sm">
          {t('settingsPage.readOnlyNotice')}
        </div>
      )}
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v })}>
        <div className="overflow-x-auto pb-1">
          <TabsList>
            {visibleTabs.map((tabItem) => (
              <TabsTrigger key={tabItem.value} value={tabItem.value}>
                {t(tabItem.labelKey)}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        {settings && (
          <>
            <TabsContent value="graph">
              <GraphTab {...tabProps} />
            </TabsContent>
            <TabsContent value="sso">
              <SsoTab {...tabProps} />
            </TabsContent>
            <TabsContent value="mail">
              <MailTab {...tabProps} />
            </TabsContent>
            <TabsContent value="schedule">
              <ScheduleTab {...tabProps} />
            </TabsContent>
            <TabsContent value="policy">
              <PolicyTab {...tabProps} />
            </TabsContent>
            <TabsContent value="alerts">
              <AlertsTab {...tabProps} />
            </TabsContent>
            <TabsContent value="branding">
              <BrandingTab {...tabProps} />
            </TabsContent>
            <TabsContent value="template">
              <TemplateTab {...tabProps} />
            </TabsContent>
            {showGeneral && (
              <TabsContent value="general">
                <GeneralTab {...tabProps} />
              </TabsContent>
            )}
          </>
        )}
      </Tabs>
    </div>
  )
}
