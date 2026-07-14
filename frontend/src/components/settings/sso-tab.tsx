import { useState } from 'react'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Switch } from '../ui/switch'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'

export function SsoTab({ settings, save, saving }: SettingsTabProps) {
  const [enabled, setEnabled] = useState(Boolean(settings['oidc.enabled'] ?? false))
  const [groupId, setGroupId] = useState(String(settings['oidc.admin_group_id'] ?? ''))
  const [label, setLabel] = useState(
    String(settings['oidc.button_label'] ?? 'Mit Microsoft anmelden'),
  )
  const [publicUrl, setPublicUrl] = useState(String(settings['app.public_url'] ?? ''))
  const base = publicUrl.trim().replace(/\/+$/, '') || window.location.origin
  const redirectUri = `${base}/api/auth/oidc/callback`

  const onSave = () =>
    save({
      'oidc.enabled': enabled,
      'oidc.admin_group_id': groupId,
      'oidc.button_label': label,
      'app.public_url': publicUrl.trim().replace(/\/+$/, ''),
    })

  return (
    <Section
      title="SSO — Anmeldung mit Microsoft"
      description="Nutzt dieselbe App-Registrierung wie Graph. Nur Mitglieder der angegebenen Entra-Gruppe dürfen sich per SSO anmelden."
      footer={
        <Button onClick={onSave} loading={saving}>
          Speichern
        </Button>
      }
    >
      <div className="border-border flex items-center justify-between rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">SSO aktivieren</p>
          <p className="text-muted-foreground text-xs">
            Blendet auf der Login-Seite den Microsoft-Button ein.
          </p>
        </div>
        <Switch checked={enabled} onCheckedChange={setEnabled} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Öffentliche App-URL (Domain)"
          hint="z. B. https://domain.example.com — bestimmt die SSO-Redirect-URI und die Links in E-Mails. Leer = lokale Adresse."
          className="sm:col-span-2"
        >
          <Input
            value={publicUrl}
            onChange={(e) => setPublicUrl(e.target.value)}
            placeholder="https://domain.example.com"
          />
        </Field>
        <Field
          label="Admin-Gruppen-Objekt-ID"
          hint="Entra → Gruppen → gewünschte Gruppe → Objekt-ID."
          className="sm:col-span-2"
        >
          <Input
            value={groupId}
            onChange={(e) => setGroupId(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono"
          />
        </Field>
        <Field label="Button-Beschriftung" className="sm:col-span-2">
          <Input value={label} onChange={(e) => setLabel(e.target.value)} />
        </Field>
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4 text-xs">
        <p className="mb-2 font-medium">In der App-Registrierung zusätzlich einrichten:</p>
        <ol className="text-muted-foreground list-decimal space-y-1 pl-4">
          <li>
            Plattform <strong>Web</strong> mit Redirect-URI:{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono break-all">{redirectUri}</code>
          </li>
          <li>
            Unter „Authentifizierung" die <strong>ID-Token</strong> aktivieren.
          </li>
          <li>
            Im <strong>Manifest</strong> <code className="font-mono">groupMembershipClaims</code>{' '}
            auf <code className="font-mono">"SecurityGroup"</code> setzen (liefert die Gruppen im
            Token).
          </li>
          <li>
            Für den <strong>SSO-Benutzer-Sync</strong> (Gruppenmitglieder lesen) unter{' '}
            <strong>API-Berechtigungen</strong> die <strong>Anwendungsberechtigung</strong>{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono">GroupMember.Read.All</code>{' '}
            hinzufügen und <strong>Administratorzustimmung erteilen</strong>. Ohne sie schlägt der
            Sync mit „403 / Insufficient privileges" fehl.
          </li>
        </ol>
        <p className="text-muted-foreground mt-3 text-xs">
          Profilbilder der SSO-Benutzer werden über die bereits benötigte Berechtigung{' '}
          <code className="bg-card rounded px-1 py-0.5 font-mono">User.Read.All</code> geladen — eine
          zusätzliche API-Berechtigung ist dafür nicht nötig.
        </p>
      </div>
    </Section>
  )
}
