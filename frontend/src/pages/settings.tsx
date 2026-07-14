import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { PageHeader } from '@/components/page-header'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api, ApiError } from '@/lib/api'
import type { Settings } from '@/lib/types'
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
  { value: 'graph', label: 'Graph' },
  { value: 'sso', label: 'SSO' },
  { value: 'mail', label: 'E-Mail' },
  { value: 'schedule', label: 'Zeitplan' },
  { value: 'policy', label: 'Passwort-Policy' },
  { value: 'branding', label: 'Branding' },
  { value: 'template', label: 'Vorlage' },
  { value: 'general', label: 'Allgemein' },
]

export default function SettingsPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') ?? 'graph'

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<Settings>('/settings'),
  })

  const mutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.put<Settings>('/settings', { values }),
    onSuccess: (data) => {
      qc.setQueryData(['settings'], data)
      void qc.invalidateQueries({ queryKey: ['branding'] })
      toast.success('Gespeichert')
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen'),
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
        title="Einstellungen"
        description="Verbindung, Versand, Zeitplan und Erscheinungsbild."
      />
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v })}>
        <div className="overflow-x-auto pb-1">
          <TabsList>
            {TABS.map((t) => (
              <TabsTrigger key={t.value} value={t.value}>
                {t.label}
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
            <TabsContent value="branding">
              <BrandingTab {...tabProps} />
            </TabsContent>
            <TabsContent value="template">
              <TemplateTab {...tabProps} />
            </TabsContent>
            <TabsContent value="general">
              <GeneralTab {...tabProps} />
            </TabsContent>
          </>
        )}
      </Tabs>
    </div>
  )
}
