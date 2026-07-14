import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpCircle, CheckCircle2, ExternalLink, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Switch } from '../ui/switch'
import { Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import type { VersionInfo } from '@/lib/types'

export function GeneralTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const [updateCheck, setUpdateCheck] = useState(Boolean(settings['app.update_check'] ?? true))
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
    </div>
  )
}
