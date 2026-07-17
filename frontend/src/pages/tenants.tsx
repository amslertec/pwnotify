import { useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/page-header'
import { CustomersTab } from '@/components/tenants/customers-tab'
import { GroupsTab } from '@/components/tenants/groups-tab'
import { SuperadminsTab } from '@/components/tenants/superadmins-tab'
import { UsersAssignmentsTab } from '@/components/tenants/users-assignments-tab'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api } from '@/lib/api'
import type { Tenant } from '@/lib/types'

/** Tab-Reihenfolge der Konsole (Task 7 der Console+Groups+Invite-Phase): mirrort das
 *  `TABS`-Array-Muster aus `settings.tsx`. Anders als dort sind alle Tabs hier IMMER
 *  sichtbar -- die Seite selbst ist bereits vollständig gegated (Superadmin +
 *  Default-Kontext, s. `SuperadminOnly`/Routing, Context-Gating v2), es gibt keine
 *  Tab-interne Sichtbarkeits-Abstufung wie bei Settings' General-Tab. */
const TABS = [
  { value: 'customers', labelKey: 'tenants.tabs.customers' },
  { value: 'assignments', labelKey: 'tenants.tabs.assignments' },
  { value: 'groups', labelKey: 'tenants.tabs.groups' },
  { value: 'superadmins', labelKey: 'tenants.tabs.superadmins' },
  { value: 'settings', labelKey: 'tenants.tabs.settings' },
] as const

const DEFAULT_TAB = TABS[0].value

/** Fällt auf den Default-Tab zurück, wenn ein `?tab=`-Deep-Link einen unbekannten Wert
 *  trägt -- sonst rendert `Tabs` keinen aktiven Trigger/Inhalt. */
export function resolveTenantsTab(requestedTab: string): string {
  return TABS.some((tabItem) => tabItem.value === requestedTab) ? requestedTab : DEFAULT_TAB
}

export default function TenantsPage() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const tab = resolveTenantsTab(params.get('tab') ?? DEFAULT_TAB)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: () => api.get<Tenant[]>('/admin/tenants'),
  })

  const tenants = data ?? []

  return (
    <div>
      <PageHeader title={t('tenants.title')} description={t('tenants.description')} />

      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v })}>
        <div className="overflow-x-auto pb-1">
          <TabsList>
            {TABS.map((tabItem) => (
              <TabsTrigger key={tabItem.value} value={tabItem.value}>
                {t(tabItem.labelKey)}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="customers">
          <CustomersTab tenants={tenants} isLoading={isLoading} />
        </TabsContent>
        <TabsContent value="assignments">
          <UsersAssignmentsTab tenants={tenants} />
        </TabsContent>
        <TabsContent value="groups">
          <GroupsTab tenants={tenants} />
        </TabsContent>
        <TabsContent value="superadmins">
          <SuperadminsTab />
        </TabsContent>
        <TabsContent value="settings">
          <SettingsTabLink />
        </TabsContent>
      </Tabs>
    </div>
  )
}

/** Einstellungen-Tab (Design §7, aufgelöst): statt Modus-Schalter/Standard-Kunden-Name
 *  HIER zu duplizieren, verlinkt dieser Tab auf den bestehenden Settings-General-Tab
 *  (`settings.tsx`, superadmin+default-context-gated, s. `showGeneralTab`) -- niedrigeres
 *  Risiko als ein zweiter Satz Steuerelemente für dieselben Instanz-Werte. */
function SettingsTabLink() {
  const { t } = useTranslation()

  return (
    <Card className="p-6">
      <h3 className="mb-1 text-sm font-semibold">{t('tenants.settingsTab.title')}</h3>
      <p className="text-muted-foreground mb-4 max-w-xl text-sm">
        {t('tenants.settingsTab.description')}
      </p>
      <Button asChild variant="outline">
        <Link to="/settings?tab=general">
          {t('tenants.settingsTab.link')} <ArrowRight className="size-4" />
        </Link>
      </Button>
    </Card>
  )
}
