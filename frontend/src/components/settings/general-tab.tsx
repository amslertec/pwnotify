import { useQuery } from '@tanstack/react-query'
import { ArrowUpCircle, CheckCircle2, ExternalLink } from 'lucide-react'
import { useState } from 'react'

import { Button } from '../ui/button'
import { Switch } from '../ui/switch'
import { Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import type { VersionInfo } from '@/lib/types'

export function GeneralTab({ settings, save, saving }: SettingsTabProps) {
  const [updateCheck, setUpdateCheck] = useState(Boolean(settings['app.update_check'] ?? true))
  const { data: ver } = useQuery({
    queryKey: ['version'],
    queryFn: () => api.get<VersionInfo>('/version'),
    staleTime: 60 * 60 * 1000,
  })

  return (
    <div className="space-y-4">
      <Section
        title="Version & Updates"
        description="Installierte Version und Update-Hinweise."
        footer={
          <Button onClick={() => save({ 'app.update_check': updateCheck })} loading={saving}>
            Speichern
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
                Installiert: {ver?.current ?? '—'}
                {ver?.update_available && ver.latest ? ` · neu verfügbar: ${ver.latest}` : ''}
              </p>
              <p className="text-muted-foreground text-xs">
                {ver && !ver.enabled
                  ? 'Update-Prüfung ist deaktiviert.'
                  : ver?.update_available
                    ? 'Ein neueres Release ist verfügbar.'
                    : 'Sie verwenden die neueste Version.'}
              </p>
            </div>
          </div>
          {ver?.update_available && (
            <a
              href={ver.release_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2"
            >
              Release ansehen <ExternalLink className="size-3.5" />
            </a>
          )}
        </div>

        <div className="border-border flex items-center justify-between rounded-lg border p-4">
          <div className="pr-3">
            <p className="text-sm font-medium">Automatisch auf Updates prüfen</p>
            <p className="text-muted-foreground text-xs">
              Vergleicht periodisch mit dem neuesten GitHub-Release und zeigt bei neuerer Version
              ein Hinweis-Fenster. Ruft dazu die öffentliche GitHub-API auf.
            </p>
          </div>
          <Switch checked={updateCheck} onCheckedChange={setUpdateCheck} />
        </div>
      </Section>
    </div>
  )
}
