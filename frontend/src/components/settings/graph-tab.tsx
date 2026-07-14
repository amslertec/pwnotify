import { useState } from 'react'
import { toast } from 'sonner'

import { EntraGuide } from '../entra-guide'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import { GraphResultCard } from '@/pages/setup'
import type { SettingsTabProps } from '@/pages/settings'
import { api, ApiError } from '@/lib/api'
import { MASK_MARKER } from '@/lib/constants'
import type { GraphTestResult } from '@/lib/types'

export function GraphTab({ settings, save, saving }: SettingsTabProps) {
  const [tenant, setTenant] = useState(String(settings['graph.tenant_id'] ?? ''))
  const [clientId, setClientId] = useState(String(settings['graph.client_id'] ?? ''))
  const [secret, setSecret] = useState('')
  const [group, setGroup] = useState(String(settings['sync.group_id'] ?? ''))
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<GraphTestResult | null>(null)
  const secretSet = settings['graph.client_secret'] === MASK_MARKER

  // Jeder Speichern-Button speichert NUR die Felder seines eigenen Abschnitts.
  const saveGraph = () =>
    save({
      'graph.tenant_id': tenant,
      'graph.client_id': clientId,
      ...(secret ? { 'graph.client_secret': secret } : {}),
    })

  const saveGroup = () => save({ 'sync.group_id': group.trim() })

  const test = async () => {
    setTesting(true)
    setResult(null)
    try {
      await saveGraph()
      const res = await api.post<GraphTestResult>('/settings/graph/test', {})
      setResult(res)
      if (res.connected && res.missing_permissions.length === 0)
        toast.success('Verbindung erfolgreich')
      else if (res.connected) toast.warning('Verbunden, aber Berechtigungen fehlen')
      else toast.error(res.error ?? 'Verbindung fehlgeschlagen')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Test fehlgeschlagen')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-4">
      <Section
        title="Microsoft Graph"
        description="App-Registrierung (Client-Credentials-Flow) für Lesen der Benutzer und Mailversand."
        footer={
          <>
            <Button variant="outline" onClick={test} loading={testing}>
              Verbindung testen
            </Button>
            <Button onClick={saveGraph} loading={saving}>
              Speichern
            </Button>
          </>
        }
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Tenant-ID" className="sm:col-span-2">
            <Input value={tenant} onChange={(e) => setTenant(e.target.value)} />
          </Field>
          <Field label="Client-ID" className="sm:col-span-2">
            <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
          </Field>
          <Field
            label="Client-Secret"
            hint={secretSet ? 'Gesetzt — leer lassen, um beizubehalten.' : undefined}
            className="sm:col-span-2"
          >
            <Input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={secretSet ? '••••••••' : ''}
            />
          </Field>
        </div>
        {result && <GraphResultCard result={result} />}
      </Section>

      <Section
        title="Sync-Umfang (Benutzergruppe)"
        description="Nur Mitglieder einer Entra-Gruppe synchronisieren und auf Passwortablauf prüfen — statt aller Tenant-Benutzer. Leer lassen = alle Benutzer."
        footer={
          <Button onClick={saveGroup} loading={saving}>
            Speichern
          </Button>
        }
      >
        <Field
          label="Gruppen-Objekt-ID"
          hint="Entra → Gruppen → gewünschte Gruppe → Objekt-ID kopieren. Verschachtelte Gruppen werden aufgelöst (transitive Mitglieder)."
        >
          <Input
            value={group}
            onChange={(e) => setGroup(e.target.value)}
            placeholder="z. B. 00000000-0000-0000-0000-000000000000"
            className="font-mono"
          />
        </Field>

        <div className="border-warning/40 bg-warning/10 text-foreground rounded-lg border p-3 text-xs">
          <p>
            <strong>Zusätzliche Berechtigung nötig:</strong> Zum Lesen der Gruppenmitglieder braucht
            die App-Registrierung die Anwendungsberechtigung{' '}
            <code className="bg-card rounded px-1 py-0.5 font-mono">GroupMember.Read.All</code>{' '}
            (Microsoft Graph, mit Administratorzustimmung) — zusätzlich zu den drei Standard-Rechten.
            Ohne sie schlägt der Sync mit „403 / Insufficient privileges" fehl.
          </p>
        </div>

        <div className="border-border bg-muted/40 text-muted-foreground space-y-3 rounded-lg border p-4 text-xs">
          <p className="text-foreground text-sm font-medium">
            Vorlage: dynamische Gruppe (erfasst Mitglieder automatisch)
          </p>
          <p>
            In Entra unter <strong>Gruppen → Neue Gruppe</strong> den Mitgliedschaftstyp{' '}
            <strong>Dynamischer Benutzer</strong> wählen (benötigt Entra ID P1) und bei{' '}
            <strong>Dynamische Abfrage → Regel-Syntax bearbeiten</strong> eine der folgenden
            Vorlagen einfügen.
          </p>

          <div className="space-y-1.5">
            <p className="text-foreground font-medium">
              1 · Basisregel — direkt einsetzbar, keine Platzhalter
            </p>
            <p>
              Erfasst nur aktive, lizenzierte Benutzer. Shared Mailboxes (ohne Lizenz) und
              deaktivierte Konten fallen automatisch heraus.
            </p>
            <pre className="bg-card text-foreground overflow-x-auto rounded-md p-3 font-mono">
              {`(user.accountEnabled -eq true) and\n(user.userType -eq "Member") and\n(user.assignedPlans -any (assignedPlan.capabilityStatus -eq "Enabled"))`}
            </pre>
          </div>

          <div className="space-y-1.5">
            <p className="text-foreground font-medium">
              2 · Vorlage mit Platzhaltern — für Produktiv anpassen
            </p>
            <p>
              Zusätzlich auf eine Domain und/oder Abteilung einschränken. Ersetze die{' '}
              <code className="bg-card rounded px-1 py-0.5 font-mono">GROSSBUCHSTABEN</code>
              -Platzhalter durch deine echten Werte:
            </p>
            <pre className="bg-card text-foreground overflow-x-auto rounded-md p-3 font-mono">
              {`(user.accountEnabled -eq true) and\n(user.userType -eq "Member") and\n(user.userPrincipalName -match "@FIRMA-DOMAIN.CH$") and\n(user.department -eq "ABTEILUNG")`}
            </pre>
            <ul className="list-disc space-y-0.5 pl-4">
              <li>
                <code className="bg-card rounded px-1 py-0.5 font-mono">FIRMA-DOMAIN.CH</code> →
                deine E-Mail-Domain
              </li>
              <li>
                <code className="bg-card rounded px-1 py-0.5 font-mono">ABTEILUNG</code> → Wert des
                Feldes <code className="font-mono">department</code> — oder diese Zeile ganz weglassen
              </li>
            </ul>
          </div>

          <p>
            Alternativ funktioniert auch eine <strong>statische Gruppe</strong>, in die du die zu
            prüfenden Benutzer manuell aufnimmst. Der Abgleich erfolgt beim nächsten Sync.
          </p>
        </div>
      </Section>

      <EntraGuide />
    </div>
  )
}
