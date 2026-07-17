import { useQueryClient } from '@tanstack/react-query'
import { Check, Database, KeyRound, Loader2, Mail, PartyPopper, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
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
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import type { GraphTestResult, SetupStatus } from '@/lib/types'
import { cn } from '@/lib/utils'

const STEPS = [
  { key: 'db', labelKey: 'setup.stepper.db', icon: Database },
  { key: 'admin', labelKey: 'setup.stepper.admin', icon: KeyRound },
  { key: 'graph', labelKey: 'setup.stepper.graph', icon: ShieldCheck },
  { key: 'mail', labelKey: 'setup.stepper.mail', icon: Mail },
  { key: 'done', labelKey: 'setup.stepper.done', icon: PartyPopper },
]

export default function SetupPage() {
  const { t } = useTranslation()
  const [step, setStep] = useState(0)
  const navigate = useNavigate()
  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1))

  return (
    <div className="bg-muted/30 min-h-full py-10">
      <div className="mx-auto w-full max-w-2xl px-4">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo />
          <h1 className="font-display mt-4 text-2xl font-semibold">{t('setup.title')}</h1>
          <p className="text-muted-foreground mt-1 text-sm">{t('setup.subtitle')}</p>
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
                  {t(s.labelKey)}
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
  const { t } = useTranslation()
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
        toast.success(t('setup.db.initialized'))
        await check()
      }
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title={t('setup.db.title')} description={t('setup.db.description')} />
      <div className="space-y-2">
        <StatusRow ok={status?.connected} label={t('setup.db.statusConnection')} busy={busy} />
        <StatusRow ok={status?.migrated} label={t('setup.db.statusSchema')} busy={busy} />
      </div>
      <div className="flex justify-between">
        {status && !status.migrated ? (
          <Button onClick={migrate} loading={busy}>
            {t('setup.db.migrateButton')}
          </Button>
        ) : (
          <span />
        )}
        <Button onClick={onNext} disabled={!status?.connected || !status?.migrated}>
          {t('setup.db.nextButton')}
        </Button>
      </div>
    </div>
  )
}

function AdminStep({ onNext }: { onNext: () => void }) {
  const { t } = useTranslation()
  const { refresh } = useAuth()
  const qc = useQueryClient()
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (password.length < 10) return toast.error(t('setup.admin.passwordTooShort'))
    if (password !== confirm) return toast.error(t('setup.admin.passwordMismatch'))
    setBusy(true)
    try {
      const display_name = `${firstName} ${lastName}`.trim() || null
      const default_tenant_name = companyName.trim() || undefined
      await api.post('/setup/admin', { username, password, display_name, default_tenant_name })
      await refresh()
      // Cache SOFORT korrigieren (nicht nur invalidieren): ab jetzt existiert ein Admin,
      // also needs_setup=false. Sonst liest der Router-Guard beim Abschluss den alten
      // gecachten true-Wert und leitet zurück auf /setup.
      qc.setQueryData<SetupStatus>(['setup-status'], (old) =>
        old ? { ...old, needs_setup: false, has_admin: true } : old,
      )
      await qc.invalidateQueries({ queryKey: ['setup-status'] })
      toast.success(t('setup.admin.created'))
      onNext()
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title={t('setup.admin.title')} description={t('setup.admin.description')} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('setup.admin.firstName')}>
          <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} autoFocus />
        </Field>
        <Field label={t('setup.admin.lastName')}>
          <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
        </Field>
        <Field label={t('setup.admin.username')} className="sm:col-span-2">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label={t('setup.admin.password')}>
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label={t('setup.admin.confirmPassword')}>
          <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        <Field
          label={t('setup.admin.companyNameLabel')}
          hint={t('setup.admin.companyNameHint')}
          className="sm:col-span-2"
        >
          <Input value={companyName} onChange={(e) => setCompanyName(e.target.value)} />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button onClick={submit} loading={busy} disabled={!username || !password}>
          {t('setup.admin.submitButton')}
        </Button>
      </div>
    </div>
  )
}

function GraphStep({ onNext }: { onNext: () => void }) {
  const { t } = useTranslation()
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
  const [ssoAuditorGroup, setSsoAuditorGroup] = useState('')
  const [ssoLabel, setSsoLabel] = useState(t('setup.graph.ssoDefaultLabel'))

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
        toast.success(t('setup.graph.testSuccess'))
      } else if (res.connected) {
        toast.warning(t('setup.graph.testWarning'))
      } else {
        toast.error(res.error ?? t('setup.graph.testFailed'))
      }
    } catch (e) {
      toast.error(translateError(e))
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
          'oidc.auditor_group_id': ssoAuditorGroup.trim(),
          'oidc.button_label': ssoLabel.trim() || t('setup.graph.ssoDefaultLabel'),
        },
      })
      onNext()
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  const base = (publicUrl.trim() || 'https://<deine-app-url>').replace(/\/$/, '')
  const redirectUri = `${base}/api/auth/oidc/callback`

  return (
    <div className="space-y-5">
      <StepHeading title={t('setup.graph.title')} description={t('setup.graph.description')} />
      <EntraGuide />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('setup.graph.tenantId')} className="sm:col-span-2">
          <Input
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </Field>
        <Field label={t('setup.graph.clientId')} className="sm:col-span-2">
          <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
        </Field>
        <Field label={t('setup.graph.clientSecret')} className="sm:col-span-2">
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
          {t('setup.graph.testButton')}
        </Button>
      </div>

      {/* Optionale Einstellungen */}
      <div className="border-border space-y-4 rounded-lg border p-4">
        <div>
          <p className="text-sm font-medium">{t('setup.graph.optionalTitle')}</p>
          <p className="text-muted-foreground text-xs">{t('setup.graph.optionalDescription')}</p>
        </div>

        <Field label={t('setup.graph.publicUrlLabel')} hint={t('setup.graph.publicUrlHint')}>
          <Input
            value={publicUrl}
            onChange={(e) => setPublicUrl(e.target.value)}
            placeholder="https://pwnotify.example.com"
          />
        </Field>

        <Field label={t('setup.graph.groupLabel')} hint={t('setup.graph.groupHint')}>
          <Input
            value={group}
            onChange={(e) => setGroup(e.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono"
          />
        </Field>

        <div className="border-border flex items-center justify-between rounded-md border p-3">
          <div className="pr-3">
            <p className="text-sm font-medium">{t('setup.graph.ssoToggleTitle')}</p>
            <p className="text-muted-foreground text-xs">{t('setup.graph.ssoToggleDescription')}</p>
          </div>
          <Switch checked={ssoEnabled} onCheckedChange={setSsoEnabled} />
        </div>

        {ssoEnabled && (
          <div className="grid gap-3">
            <Field label={t('setup.graph.ssoGroupLabel')} hint={t('setup.graph.ssoGroupHint')}>
              <Input
                value={ssoGroup}
                onChange={(e) => setSsoGroup(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className="font-mono"
              />
            </Field>
            <Field
              label={t('setup.graph.ssoAuditorGroupLabel')}
              hint={t('setup.graph.ssoAuditorGroupHint')}
            >
              <Input
                value={ssoAuditorGroup}
                onChange={(e) => setSsoAuditorGroup(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                className="font-mono"
              />
            </Field>
            <Field label={t('setup.graph.ssoLabelLabel')}>
              <Input value={ssoLabel} onChange={(e) => setSsoLabel(e.target.value)} />
            </Field>
            <div className="text-muted-foreground bg-muted/40 rounded-md p-3 text-xs">
              {t('setup.graph.redirectHint')}{' '}
              <code className="bg-card rounded px-1 py-0.5 font-mono break-all">{redirectUri}</code>
            </div>
          </div>
        )}
      </div>

      <div className="flex justify-end">
        <Button onClick={proceed} loading={busy} disabled={!result?.connected}>
          {t('setup.graph.nextButton')}
        </Button>
      </div>
    </div>
  )
}

function MailStep({ onNext }: { onNext: () => void }) {
  const { t } = useTranslation()
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
      toast.success(t('setup.mail.testSent', { to: testTo }))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <StepHeading title={t('setup.mail.title')} description={t('setup.mail.description')} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('setup.mail.backendLabel')}>
          <Select value={backend} onValueChange={setBackend}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="graph">{t('setup.mail.backendGraph')}</SelectItem>
              <SelectItem value="smtp">{t('setup.mail.backendSmtp')}</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label={t('setup.mail.fromLabel')}>
          <Input
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            placeholder="noreply@example.com"
          />
        </Field>
        {backend === 'smtp' && (
          <>
            <Field label={t('setup.mail.smtpHost')}>
              <Input value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
            </Field>
            <Field label={t('setup.mail.smtpPort')}>
              <Input value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} />
            </Field>
            <Field label={t('setup.mail.smtpUser')}>
              <Input value={smtpUser} onChange={(e) => setSmtpUser(e.target.value)} />
            </Field>
            <Field label={t('setup.mail.smtpPass')}>
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
        <Label className="text-xs">{t('setup.mail.testLabel')}</Label>
        <div className="mt-2 flex gap-2">
          <Input
            value={testTo}
            onChange={(e) => setTestTo(e.target.value)}
            placeholder={t('setup.mail.testPlaceholder')}
          />
          <Button variant="outline" onClick={sendTest} loading={busy} disabled={!testTo || !from}>
            {t('setup.mail.sendButton')}
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
          {t('setup.mail.skipButton')}
        </Button>
        <Button
          onClick={async () => {
            await save()
            toast.success(t('setup.mail.saved'))
            onNext()
          }}
          disabled={!from}
        >
          {t('setup.mail.saveButton')}
        </Button>
      </div>
    </div>
  )
}

function DoneStep({ onFinish }: { onFinish: () => void }) {
  const { t } = useTranslation()
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
        <h2 className="font-display text-xl font-semibold">{t('setup.done.heading')}</h2>
        <p className="text-muted-foreground mt-1 max-w-sm text-sm">{t('setup.done.description')}</p>
      </div>
      <div className="text-muted-foreground flex items-center gap-2 text-sm">
        {syncing ? (
          <>
            <Loader2 className="size-4 animate-spin" /> {t('setup.done.syncing')}
          </>
        ) : (
          <>
            <Check className="text-success size-4" /> {t('setup.done.syncDone')}
          </>
        )}
      </div>
      <Button onClick={onFinish}>{t('setup.done.dashboardButton')}</Button>
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
  const { t } = useTranslation()
  return (
    <div
      className={cn(
        'rounded-lg border p-4',
        result.connected ? 'border-success/40 bg-success/5' : 'border-danger/40 bg-danger/5',
      )}
    >
      <p className="text-sm font-medium">
        {result.connected ? t('setup.result.connected') : t('setup.result.failed')}
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
                {!granted && <span className="text-danger">{t('setup.result.missing')}</span>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
