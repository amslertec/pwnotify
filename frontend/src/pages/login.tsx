import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Logo } from '@/components/logo'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { IDLE_LOGOUT_FLAG, useAuth } from '@/lib/auth'
import { useBranding } from '@/components/branding-provider'
import type { AuthConfig, SetupStatus, TwoFactorSetup } from '@/lib/types'

const schema = z.object({
  username: z.string().min(1, 'validation.usernameRequired'),
  password: z.string().min(1, 'validation.passwordRequired'),
})
type FormValues = z.infer<typeof schema>

function MicrosoftIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 21 21" aria-hidden>
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  )
}

export default function LoginPage() {
  const { user, login, verify2fa, refresh } = useAuth()
  const { branding } = useBranding()
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [email, setEmail] = useState('')
  const [twoFactor, setTwoFactor] = useState(false)
  // 2FA ist Pflicht, aber noch nicht eingerichtet -> Einrichtung direkt hier, es gibt
  // noch keine Sitzung. Ohne diesen Schritt käme man nirgends hin.
  const [enroll, setEnroll] = useState<TwoFactorSetup | null>(null)
  const [recovery, setRecovery] = useState<string[] | null>(null)
  const [code, setCode] = useState('')
  const [verifying, setVerifying] = useState(false)

  const { data: setup } = useQuery({
    queryKey: ['setup-status'],
    queryFn: () => api.get<SetupStatus>('/setup/status'),
  })
  const { data: authCfg } = useQuery({
    queryKey: ['auth-config'],
    queryFn: () => api.get<AuthConfig>('/auth/config'),
  })
  const ssoEnabled = authCfg?.oidc_enabled ?? false
  const [showLocal, setShowLocal] = useState(false)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  useEffect(() => {
    if (params.get('sso_denied')) toast.error(t('login.ssoDenied'))
    if (params.get('sso_error')) toast.error(t('login.ssoError'))
    // Erklärt die automatische Abmeldung — sonst wirkt sie wie ein Fehler.
    if (sessionStorage.getItem(IDLE_LOGOUT_FLAG)) {
      sessionStorage.removeItem(IDLE_LOGOUT_FLAG)
      toast.info(t('login.idleLogout'))
    }
  }, [params, t])

  if (setup?.needs_setup) return <Navigate to="/setup" replace />
  if (user) return <Navigate to="/" replace />

  const onSubmit = async (values: FormValues) => {
    try {
      const res = await login(values.username, values.password)
      if (res.two_factor_required) setTwoFactor(true)
      else if (res.two_factor_setup_required) {
        // Der 2FA-Zwischentoken erlaubt genau das: einrichten.
        setEnroll(await api.post<TwoFactorSetup>('/auth/2fa/setup'))
      } else navigate('/')
    } catch (e) {
      toast.error(translateError(e))
    }
  }

  const submitEnroll = async () => {
    setVerifying(true)
    try {
      const res = await api.post<{ recovery_codes: string[] }>('/auth/2fa/enable', {
        code: code.trim(),
      })
      // Aktivieren stellt zugleich die Sitzung aus. Die Codes zeigen wir einmalig —
      // danach sind sie nicht mehr abrufbar.
      setRecovery(res.recovery_codes)
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setVerifying(false)
    }
  }

  const submit2fa = async () => {
    setVerifying(true)
    try {
      await verify2fa(code.trim())
      navigate('/')
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setVerifying(false)
    }
  }

  const emailValid = /.+@.+\..+/.test(email)
  const localVisible = !ssoEnabled || showLocal

  return (
    <div className="grid min-h-full lg:grid-cols-2">
      {/* Brand-Panel */}
      <div className="relative hidden overflow-hidden bg-[#0b0f19] lg:flex lg:flex-col lg:justify-between lg:p-12">
        <div
          className="pointer-events-none absolute top-1/3 -left-24 size-[32rem] rounded-full opacity-30 blur-3xl"
          style={{ background: 'radial-gradient(circle, #4F46E5, transparent 70%)' }}
        />
        <div
          className="pointer-events-none absolute -right-20 bottom-0 size-[26rem] rounded-full opacity-20 blur-3xl"
          style={{ background: 'radial-gradient(circle, #F59E0B, transparent 70%)' }}
        />
        <img
          src={
            branding.has_logo
              ? `/api/branding/logo?v=${branding.logo_version}`
              : '/brand/logo-dark.svg'
          }
          alt={branding.app_name}
          className="relative h-14 w-auto self-start"
        />
        <div className="relative max-w-md">
          <h2 className="font-display text-3xl leading-tight font-semibold text-slate-100">
            {t('login.brandHeadline')}
          </h2>
          <p className="mt-3 text-slate-400">{t('login.brandSubtext')}</p>
        </div>
        <p className="relative text-xs text-slate-500">
          {branding.company_name || branding.app_name}
        </p>
      </div>

      {/* Formular */}
      <div className="flex items-center justify-center p-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <Logo className="h-11 w-auto" />
          </div>
          <div className="text-primary mb-6 flex items-center gap-2">
            <ShieldCheck className="size-5" />
            <span className="text-sm font-medium">{t('login.signInTag')}</span>
          </div>
          <h1 className="font-display text-2xl font-semibold">{t('login.welcomeBack')}</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            {t('login.signInPrompt', { app: branding.app_name })}
          </p>

          {/* Erzwungene 2FA-Einrichtung: Passwort stimmt, aber ohne zweiten Faktor
              gibt es keine Sitzung. */}
          {enroll && !recovery && (
            <div className="mt-8 space-y-4">
              <p className="text-muted-foreground text-sm">{t('login.enroll.prompt')}</p>
              <div className="bg-card flex justify-center rounded-lg border p-4">
                <img src={enroll.qr_png} alt="" className="size-44" />
              </div>
              <p className="text-muted-foreground text-xs">
                {t('login.enroll.manual')}{' '}
                <code className="bg-muted rounded px-1 py-0.5 font-mono">{enroll.secret}</code>
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="enroll-code">{t('login.codeLabel')}</Label>
                <Input
                  id="enroll-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder={t('login.codePlaceholder')}
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submitEnroll()}
                />
              </div>
              <Button
                className="w-full"
                loading={verifying}
                onClick={submitEnroll}
                disabled={!code.trim()}
              >
                {t('login.enroll.activateButton')}
              </Button>
            </div>
          )}

          {/* Recovery-Codes: einmalig sichtbar, danach nie wieder. */}
          {recovery && (
            <div className="mt-8 space-y-4">
              <p className="text-sm font-medium">{t('login.enroll.recoveryTitle')}</p>
              <p className="text-muted-foreground text-sm">{t('login.enroll.recoveryHint')}</p>
              <div className="bg-muted grid grid-cols-2 gap-1 rounded-lg p-3 font-mono text-xs">
                {recovery.map((c) => (
                  <span key={c}>{c}</span>
                ))}
              </div>
              <Button
                className="w-full"
                onClick={async () => {
                  await refresh()
                  navigate('/')
                }}
              >
                {t('login.enroll.continueButton')}
              </Button>
            </div>
          )}

          {/* 2FA-Schritt */}
          {twoFactor && !enroll && (
            <div className="mt-8 space-y-4">
              <p className="text-muted-foreground text-sm">{t('login.twoFactorPrompt')}</p>
              <div className="space-y-1.5">
                <Label htmlFor="tfa">{t('login.codeLabel')}</Label>
                <Input
                  id="tfa"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder={t('login.codePlaceholder')}
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submit2fa()}
                />
              </div>
              <Button
                className="w-full"
                loading={verifying}
                onClick={submit2fa}
                disabled={!code.trim()}
              >
                {t('login.verifyButton')}
              </Button>
              <p className="text-muted-foreground text-xs">{t('login.recoveryHint')}</p>
            </div>
          )}

          {/* SSO-Block */}
          {!twoFactor && !enroll && ssoEnabled && (
            <div className="mt-8 space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="email">{t('login.emailLabel')}</Label>
                <Input
                  id="email"
                  type="email"
                  autoComplete="email"
                  placeholder={t('login.emailPlaceholder')}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              {emailValid && (
                <Button
                  variant="outline"
                  className="w-full"
                  onClick={() => {
                    window.location.href = `/api/auth/oidc/login?login_hint=${encodeURIComponent(email)}`
                  }}
                >
                  <MicrosoftIcon /> {authCfg?.oidc_button_label || t('login.microsoftButton')}
                </Button>
              )}
              <button
                type="button"
                onClick={() => setShowLocal((v) => !v)}
                className="text-muted-foreground hover:text-foreground text-xs"
              >
                {showLocal ? t('login.hideLocal') : t('login.useLocalAccount')}
              </button>
            </div>
          )}

          {/* Lokaler Login */}
          {!twoFactor && !enroll && localVisible && (
            <form
              onSubmit={handleSubmit(onSubmit)}
              className={
                ssoEnabled ? 'border-border mt-4 space-y-4 border-t pt-4' : 'mt-8 space-y-4'
              }
            >
              <div className="space-y-1.5">
                <Label htmlFor="username">{t('login.usernameLabel')}</Label>
                <Input id="username" autoComplete="username" {...register('username')} />
                {errors.username && (
                  <p className="text-danger text-xs">{t(errors.username.message ?? '')}</p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">{t('login.passwordLabel')}</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  {...register('password')}
                />
                {errors.password && (
                  <p className="text-danger text-xs">{t(errors.password.message ?? '')}</p>
                )}
              </div>
              <Button type="submit" className="w-full" loading={isSubmitting}>
                {t('login.signInButton')}
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}
