import { useQueryClient } from '@tanstack/react-query'
import { RotateCcw, Trash2, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'

import { useBranding } from '../branding-provider'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api, ApiError, uploadFile } from '@/lib/api'

const DEFAULT_COLOR = '#4F46E5'

export function BrandingTab({ settings, save, saving }: SettingsTabProps) {
  const qc = useQueryClient()
  const { branding, refetch } = useBranding()
  const [appName, setAppName] = useState(String(settings['branding.app_name'] ?? 'PwNotify'))
  const [company, setCompany] = useState(String(settings['branding.company_name'] ?? ''))
  const [color, setColor] = useState(String(settings['branding.primary_color'] ?? '#4F46E5'))
  const [resetUrl, setResetUrl] = useState(String(settings['branding.reset_url'] ?? ''))
  const logoRef = useRef<HTMLInputElement>(null)
  const faviconRef = useRef<HTMLInputElement>(null)

  const onSave = async () => {
    await save({
      'branding.app_name': appName,
      'branding.company_name': company,
      'branding.primary_color': color,
      'branding.reset_url': resetUrl,
    })
    refetch()
  }

  const reload = async () => {
    await qc.invalidateQueries({ queryKey: ['branding'] })
    await qc.invalidateQueries({ queryKey: ['settings'] })
    refetch()
  }

  const upload = async (kind: 'logo' | 'favicon', file?: File) => {
    if (!file) return
    try {
      await uploadFile(`/branding/${kind}`, file)
      toast.success('Hochgeladen')
      await reload()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Upload fehlgeschlagen')
    }
  }

  const remove = async (kind: 'logo' | 'favicon') => {
    try {
      await api.del(`/branding/${kind}`)
      toast.success('Entfernt — Standard aktiv.')
      await reload()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Entfernen fehlgeschlagen')
    }
  }

  return (
    <Section
      title="Branding"
      description="Erscheinungsbild in App, Login und E-Mails."
      footer={
        <Button onClick={onSave} loading={saving}>
          Speichern
        </Button>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="App-Name">
          <Input value={appName} onChange={(e) => setAppName(e.target.value)} />
        </Field>
        <Field label="Firmenname">
          <Input value={company} onChange={(e) => setCompany(e.target.value)} />
        </Field>
        <Field label="Primärfarbe" hint="Setzt das Farbschema der gesamten App inkl. Diagramme.">
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={color}
              onChange={(e) => setColor(e.target.value)}
              className="border-border size-9 cursor-pointer rounded-md border bg-transparent"
              aria-label="Farbe wählen"
            />
            <Input value={color} onChange={(e) => setColor(e.target.value)} className="font-mono" />
            <Button
              variant="outline"
              size="icon"
              onClick={() => setColor(DEFAULT_COLOR)}
              title="Auf Standardfarbe zurücksetzen"
              aria-label="Farbe zurücksetzen"
              disabled={color.toLowerCase() === DEFAULT_COLOR.toLowerCase()}
            >
              <RotateCcw className="size-4" />
            </Button>
          </div>
        </Field>
        <Field label="Passwort-Reset-URL" hint="Ziel des Buttons in der E-Mail.">
          <Input value={resetUrl} onChange={(e) => setResetUrl(e.target.value)} />
        </Field>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="border-border rounded-lg border p-4">
          <p className="mb-2 text-sm font-medium">Logo</p>
          {settings['branding.logo_path'] ? (
            <img
              src={`/api/branding/logo?v=${branding.logo_version}`}
              alt="Logo"
              className="mb-3 h-9 max-w-[190px] object-contain"
            />
          ) : (
            <p className="text-muted-foreground mb-3 text-xs">Standard-Logo aktiv</p>
          )}
          <input
            ref={logoRef}
            type="file"
            accept="image/png,image/svg+xml,image/webp"
            className="hidden"
            onChange={(e) => upload('logo', e.target.files?.[0])}
          />
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => logoRef.current?.click()}>
              <Upload /> Logo hochladen
            </Button>
            {settings['branding.logo_path'] ? (
              <Button variant="outline" size="sm" onClick={() => remove('logo')}>
                <Trash2 className="text-danger size-4" /> Entfernen
              </Button>
            ) : null}
          </div>
        </div>
        <div className="border-border rounded-lg border p-4">
          <p className="mb-2 text-sm font-medium">Favicon</p>
          {settings['branding.favicon_path'] ? (
            <img
              src={`/api/branding/favicon?v=${branding.favicon_version}`}
              alt="Favicon"
              className="mb-3 size-8 object-contain"
            />
          ) : (
            <p className="text-muted-foreground mb-3 text-xs">Standard-Favicon aktiv</p>
          )}
          <input
            ref={faviconRef}
            type="file"
            accept="image/png,image/svg+xml,image/x-icon"
            className="hidden"
            onChange={(e) => upload('favicon', e.target.files?.[0])}
          />
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => faviconRef.current?.click()}>
              <Upload /> Favicon hochladen
            </Button>
            {settings['branding.favicon_path'] ? (
              <Button variant="outline" size="sm" onClick={() => remove('favicon')}>
                <Trash2 className="text-danger size-4" /> Entfernen
              </Button>
            ) : null}
          </div>
        </div>
      </div>
    </Section>
  )
}
