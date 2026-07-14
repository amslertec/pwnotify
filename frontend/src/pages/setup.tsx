import { useQueryClient } from '@tanstack/react-query'
import { Check, Database, KeyRound, Loader2, Mail, PartyPopper, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { EntraGuide } from '@/components/entra-guide'
import { Logo } from '@/components/logo'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import type { GraphTestResult, SetupStatus } from '@/lib/types'
import { cn } from '@/lib/utils'

const STEPS = [
  { key: 'db', label: 'Datenbank', icon: Database },
  { key: 'admin', label: 'Administrator', icon: KeyRound },
  { key: 'graph', label: 'Microsoft Graph', icon: ShieldCheck },
  { key: 'mail', label: 'E-Mail', icon: Mail },
  { key: 'done', label: 'Fertig', icon: PartyPopper },
]

export default function SetupPage() {
  const [step, setStep] = useState(0)
  const navigate = useNavigate()
  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1))

  return (
    <div className="bg-muted/30 min-h-full py-10">
      <div className="mx-auto w-full max-w-2xl px-4">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo />
          <h1 className="font-display mt-4 text-2xl font-semibold">Ersteinrichtung</h1>
          <p className="text-muted-foreground mt-1 text-sm">In wenigen Schritten einsatzbereit.</p>
        </div>

        {/* Stepper */}
        <ol className="mb-8 flex items-center justify-between">
          {STEPS.map((s, i) => (
            <li key={s.key} className="flex flex-1 items-center">
              <div className="flex flex-col items-center gap-1.5">
                <span
                  className={cn(
                    'grid size-9 place-items-center rounded-full border-2 transition-colors',
                    i < step && 'border-primary bg-primary text-primary-foreground',
                    i === step && 'border-primary text-primary',
                    i > step && 'border-border text-muted-foreground',
                  )}
                >
                  {i < step ? <Check className="size-4" /> : <s.icon className="size-4" />}
                </span>
                <span
                  className={cn(
                    'hidden text-xs sm:block',
                    i === step ? 'text-foreground font-medium' : 'text-muted-foreground',
                  )}
                >
                  {s.label}
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <div className={cn('mx-2 h-0.5 flex-1', i < step ? 'bg-primary' : 'bg-border')} />
              )}
            </li>
          ))}
        </ol>

        <div className="border-border bg-card rounded-xl border p-6 shadow-sm">
          {step === 0 && <DatabaseStep onNext={next} />}
          {step === 1 && <AdminStep onNext={next} />}
          {step === 2 && <GraphStep onNext={next} />}
          {step === 3 && <MailStep onNext={next} />}
          {step === 4 && <DoneStep onFinish={() => navigate('/')} />}
        </div>
      </div>
    </div>
  )
}

function DatabaseStep({ onNext }: { onNext: () => void }) {
  const [status, setStatus] = useState<{ connected: boolean; migrated: boolean } | null>(null)
  const [busy, setBusy] = useState(false)

  const check = async () => {
    setBusy(true)
    try {
      setStatus(await api.post('/setup/database/test'))
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    void check()
  }, [])

  const migrate = async () => {
    setBusy(true)
    try {
      const res = await api.post<{ migrated: boolean }>('/setup/database/migrate')
      if (res.migrated) {
        toast.success('Datenbank initialisiert')
        await check()
      }
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Migration fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title="Datenbank" description="Verbindung prüfen und Schema initialisieren." />
      <div className="space-y-2">
        <StatusRow ok={status?.connected} label="Verbindung zur PostgreSQL-Datenbank" busy={busy} />
        <StatusRow ok={status?.migrated} label="Datenbankschema (Migrationen)" busy={busy} />
      </div>
      <div className="flex justify-between">
        {status && !status.migrated ? (
          <Button onClick={migrate} loading={busy}>
            Migrationen anwenden
          </Button>
        ) : (
          <span />
        )}
        <Button onClick={onNext} disabled={!status?.connected || !status?.migrated}>
          Weiter
        </Button>
      </div>
    </div>
  )
}

function AdminStep({ onNext }: { onNext: () => void }) {
  const { refresh } = useAuth()
  const qc = useQueryClient()
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (password.length < 10) return toast.error('Passwort mind. 10 Zeichen')
    if (password !== confirm) return toast.error('Passwörter stimmen nicht überein')
    setBusy(true)
    try {
      const display_name = `${firstName} ${lastName}`.trim() || null
      await api.post('/setup/admin', { username, password, display_name })
      await refresh()
      // Cache SOFORT korrigieren (nicht nur invalidieren): ab jetzt existiert ein Admin,
      // also needs_setup=false. Sonst liest der Router-Guard beim Abschluss den alten
      // gecachten true-Wert und leitet zurück auf /setup.
      qc.setQueryData<SetupStatus>(['setup-status'], (old) =>
        old ? { ...old, needs_setup: false, has_admin: true } : old,
      )
      await qc.invalidateQueries({ queryKey: ['setup-status'] })
      toast.success('Administrator angelegt')
      onNext()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Anlegen fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title="Administrator anlegen" description="Dieses Konto verwaltet PwNotify." />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Vorname">
          <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} autoFocus />
        </Field>
        <Field label="Nachname">
          <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
        </Field>
        <Field label="Benutzername" className="sm:col-span-2">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Passwort (mind. 10 Zeichen)">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Passwort bestätigen">
          <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button onClick={submit} loading={busy} disabled={!username || !password}>
          Konto erstellen & weiter
        </Button>
      </div>
    </div>
  )
}

function GraphStep({ onNext }: { onNext: () => void }) {
  const [tenant, setTenant] = useState('')
  const [clientId, setClientId] = useState('')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<GraphTestResult | null>(null)

  // Optionale Einstellungen — können auch später in den Einstellungen gesetzt werden.
  const [publicUrl, setPublicUrl] = useState('')
  const [group, setGroup] = useState('')
  const [ssoEnabled, setSsoEnabled] = useState(false)
  const [ssoGroup, setSsoGroup] = useState('')
  const [ssoLabel, setSsoLabel] = useState('Mit Microsoft anmelden')

  const saveTest = async () => {
    setBusy(true)
    setResult(null)
    try {
      await api.put('/settings', {
        values: {
          'graph.tenant_id': tenant,
          'graph.client_id': clientId,
          'graph.client_secret': secret,
        },
      })
      const res = await api.post<GraphTestResult>('/settings/graph/test', {})
      setResult(res)
      if (res.connected && res.missing_permissions.length === 0) {
        toast.success('Graph-Verbindung erfolgreich')
      } else if (res.connected) {
        toast.warning('Verbunden, aber Berechtigungen fehlen')
      } else {
        toast.error(res.error ?? 'Verbindung fehlgeschlagen')
      }
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Test fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const proceed = async () => {
    setBusy(true)
    try {
      await api.put('/settings', {
        values: {
          'app.public_url': publicUrl.trim(),
          'sync.group_id': group.trim(),
          'oidc.enabled': ssoEnabled,
          'oidc.admin_group_id': ssoGroup.trim(),
          'oidc.button_label': ssoLabel.trim() || 'Mit Microsoft anmelden',
        },
      })
      onNext()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const base = (publicUrl.trim() || 'https://<deine-app-url>').replace(/\/$/, '')
  const redirectUri = `${base}/api/auth/oidc/callback`

  return (
    <div className="space-y-5">
      <StepHeading
        title="Microsoft Graph verbinden"
        description="Ohne diese Verbindung kann PwNotify keine Benutzer lesen oder Mails senden."
      />
      <EntraGuide />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Verzeichnis-(Mandanten-)ID" className="sm:col-span-2">
          <Input
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </Field>
        <Field label="Anwendungs-(Client-)ID" className="sm:col-span-2">
          <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
        </Field>
        <Field label="Geheimer Clientschlüssel" className="sm:col-span-2">
          <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
        </Field>
      </div>

      {result && <GraphResultCard result={result} />}

      <div className="flex justify-end">
        <Button
          variant="outline"
          onClick={saveTest}
          loading={busy}
          disabled={!tenant || !clientId || !secret}
        >
          Speichern & Verbindung testen
        </Button>
      </div>

      {/* Optionale Einstellungen */}
      <div className="border-border space-y-4 rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">Optionale Einstellungen</p>
          <p className="text-muted-foreground text-xs">
            Alles hier ist optional und lässt sich später in den Einstellungen ändern.
          </p>
        </div>

        <Field
          label="Öffentliche App-URL (Domain)"
          hint="Für Links in E-Mails und den SSO-Redirect. Leer lassen = interne URL verwenden."
        >
          <Input
            value={publicUrl}
            onChange={(e) => setPublicUrl(e.target.value)}
            placeholder="https://pwnotify.example.com"
          />
        </Field>

        <Field
          label="Sync-Umfang: Gruppen-Objekt-ID"
          hint="Nur Mitglieder dieser Entra-Gruppe synchronisieren. Leer lassen = alle Benutzer. Benötigt GroupMember.Read.All."
        >
          <Input
            value={group}
            onChange={(e) => setGroup(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono"
          />
        </Field>

        <div className="border-border flex items-center justify-between rounded-md border p-3">
          <div className="pr-3">
            <p className="text-sm font-medium">Microsoft-SSO-Anmeldung aktivieren</p>
            <p className="text-muted-foreground text-xs">
              Login per Microsoft-Konto — nutzt dieselbe App-Registrierung. Benötigt
              GroupMember.Read.All und den groups-Claim im Token.
            </p>
          </div>
          <Switch checked={ssoEnabled} onCheckedChange={setSsoEnabled} />
        </div>

        {ssoEnabled && (
          <div className="grid gap-3">
            <Field
              label="Admin-Gruppen-Objekt-ID"
              hint="Nur Mitglieder dieser Gruppe dürfen sich per SSO anmelden."
            >
              <Input
                value={ssoGroup}
                onChange={(e) => setSsoGroup(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className="font-mono"
              />
            </Field>
            <Field label="Button-Beschriftung">
              <Input value={ssoLabel} onChange={(e) => setSsoLabel(e.target.value)} />
            </Field>
            <div className="text-muted-foreground bg-muted/40 rounded-md p-3 text-xs">
              Diese Redirect-URI in der Entra-App-Registrierung (Plattform Web) hinterlegen:{' '}
              <code className="bg-card rounded px-1 py-0.5 font-mono break-all">{redirectUri}</code>
            </div>
          </div>
        )}
      </div>

      <div className="flex justify-end">
        <Button onClick={proceed} loading={busy} disabled={!result?.connected}>
          Weiter
        </Button>
      </div>
    </div>
  )
}

function MailStep({ onNext }: { onNext: () => void }) {
  const [backend, setBackend] = useState('graph')
  const [from, setFrom] = useState('')
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('587')
  const [smtpUser, setSmtpUser] = useState('')
  const [smtpPass, setSmtpPass] = useState('')
  const [testTo, setTestTo] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    await api.put('/settings', {
      values: {
        'mail.backend': backend,
        'mail.from': from,
        ...(backend === 'smtp'
          ? {
              'mail.smtp_host': smtpHost,
              'mail.smtp_port': Number(smtpPort),
              'mail.smtp_username': smtpUser,
              'mail.smtp_password': smtpPass,
            }
          : {}),
      },
    })
  }

  const sendTest = async () => {
    setBusy(true)
    try {
      await save()
      await api.post('/settings/mail/test', { to: testTo, locale: 'de' })
      toast.success(`Test-Mail an ${testTo} gesendet`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Versand fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title="E-Mail-Versand" description="Wie sollen Erinnerungen versendet werden?" />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Backend">
          <Select value={backend} onValueChange={setBackend}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="graph">Microsoft Graph (sendMail)</SelectItem>
              <SelectItem value="smtp">SMTP-Server</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label="Absenderadresse">
          <Input
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            placeholder="noreply@example.com"
          />
        </Field>
        {backend === 'smtp' && (
          <>
            <Field label="SMTP-Host">
              <Input value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
            </Field>
            <Field label="Port">
              <Input value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} />
            </Field>
            <Field label="Benutzer">
              <Input value={smtpUser} onChange={(e) => setSmtpUser(e.target.value)} />
            </Field>
            <Field label="Passwort">
              <Input
                type="password"
                value={smtpPass}
                onChange={(e) => setSmtpPass(e.target.value)}
              />
            </Field>
          </>
        )}
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4">
        <Label className="text-xs">Optional: Test-Mail senden</Label>
        <div className="mt-2 flex gap-2">
          <Input
            value={testTo}
            onChange={(e) => setTestTo(e.target.value)}
            placeholder="ihre.adresse@example.com"
          />
          <Button variant="outline" onClick={sendTest} loading={busy} disabled={!testTo || !from}>
            Senden
          </Button>
        </div>
      </div>

      <div className="flex justify-end gap-2">
        <Button
          variant="ghost"
          onClick={async () => {
            await save()
            onNext()
          }}
        >
          Überspringen
        </Button>
        <Button
          onClick={async () => {
            await save()
            toast.success('Mail-Einstellungen gespeichert')
            onNext()
          }}
          disabled={!from}
        >
          Speichern & weiter
        </Button>
      </div>
    </div>
  )
}

function DoneStep({ onFinish }: { onFinish: () => void }) {
  const [syncing, setSyncing] = useState(true)

  // Erst-Sync automatisch starten: Benutzer (dry-run -> befüllen ohne Mailversand)
  // und SSO-Benutzer (No-op, falls SSO nicht aktiviert wurde).
  useEffect(() => {
    let active = true
    void (async () => {
      try {
        await api.post('/runs/trigger', { dry_run: true })
      } catch {
        /* Erst-Sync ist best-effort; jederzeit manuell wiederholbar */
      }
      try {
        await api.post('/admin/users/sso/sync')
      } catch {
        /* SSO-Sync nur relevant, wenn SSO konfiguriert ist */
      }
      if (active) setSyncing(false)
    })()
    return () => {
      active = false
    }
  }, [])

  return (
    <div className="flex flex-col items-center gap-4 py-6 text-center">
      <div className="bg-success/15 text-success grid size-14 place-items-center rounded-2xl">
        <PartyPopper className="size-7" />
      </div>
      <div>
        <h2 className="font-display text-xl font-semibold">Alles bereit!</h2>
        <p className="text-muted-foreground mt-1 max-w-sm text-sm">
          PwNotify ist eingerichtet. Der Erst-Sync läuft automatisch — Benutzer und SSO-Benutzer
          werden geladen. Es werden noch keine E-Mails versendet; das übernimmt der Zeitplan.
        </p>
      </div>
      <div className="text-muted-foreground flex items-center gap-2 text-sm">
        {syncing ? (
          <>
            <Loader2 className="size-4 animate-spin" /> Erst-Sync läuft…
          </>
        ) : (
          <>
            <Check className="text-success size-4" /> Erst-Sync abgeschlossen
          </>
        )}
      </div>
      <Button onClick={onFinish}>Zum Dashboard</Button>
    </div>
  )
}

/* ---- kleine Helfer ---- */
function StepHeading({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <h2 className="font-display text-lg font-semibold">{title}</h2>
      <p className="text-muted-foreground mt-1 text-sm">{description}</p>
    </div>
  )
}

function Field({
  label,
  children,
  className,
  hint,
}: {
  label: string
  children: React.ReactNode
  className?: string
  hint?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
    </div>
  )
}

function StatusRow({ ok, label, busy }: { ok?: boolean; label: string; busy?: boolean }) {
  return (
    <div className="border-border bg-card flex items-center gap-3 rounded-lg border px-4 py-3">
      {busy && ok === undefined ? (
        <Loader2 className="text-muted-foreground size-5 animate-spin" />
      ) : ok ? (
        <span className="bg-success grid size-5 place-items-center rounded-full text-white">
          <Check className="size-3.5" />
        </span>
      ) : (
        <span className="border-danger size-5 rounded-full border-2" />
      )}
      <span className="text-sm">{label}</span>
    </div>
  )
}

export function GraphResultCard({ result }: { result: GraphTestResult }) {
  return (
    <div
      className={cn(
        'rounded-lg border p-4',
        result.connected ? 'border-success/40 bg-success/5' : 'border-danger/40 bg-danger/5',
      )}
    >
      <p className="text-sm font-medium">
        {result.connected ? 'Verbindung hergestellt' : 'Verbindung fehlgeschlagen'}
      </p>
      {result.error && <p className="text-danger mt-1 text-xs">{result.error}</p>}
      {result.connected && (
        <div className="mt-3 space-y-1.5">
          {['User.Read.All', 'Domain.Read.All', 'Mail.Send'].map((p) => {
            const granted = result.granted_permissions.includes(p)
            return (
              <div key={p} className="flex items-center gap-2 text-xs">
                {granted ? (
                  <Check className="text-success size-3.5" />
                ) : (
                  <span className="border-danger size-3.5 rounded-full border" />
                )}
                <code className="font-mono">{p}</code>
                {!granted && <span className="text-danger">— fehlt / kein Admin-Consent</span>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
