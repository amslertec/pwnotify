import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Section } from './section'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { useAuth } from '@/lib/auth'
import type { TwoFactorSetup } from '@/lib/types'

/** 2FA-Verwaltung auf der Profilseite (nur lokale Konten). */
export function TwoFactorSection() {
  const { t } = useTranslation()
  const { user, refresh } = useAuth()
  const [setup, setSetup] = useState<TwoFactorSetup | null>(null)
  const [recovery, setRecovery] = useState<string[] | null>(null)
  const [disabling, setDisabling] = useState(false)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)

  if (user?.is_sso) return null // SSO-Konten nutzen die Entra-Anmeldung

  const startSetup = async () => {
    setBusy(true)
    try {
      setSetup(await api.post<TwoFactorSetup>('/auth/2fa/setup'))
      setCode('')
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  const enable = async () => {
    setBusy(true)
    try {
      const res = await api.post<{ recovery_codes: string[] }>('/auth/2fa/enable', {
        code: code.trim(),
      })
      setRecovery(res.recovery_codes)
      setSetup(null)
      setCode('')
      await refresh()
      toast.success(t('twofa.enabled'))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  const disable = async () => {
    setBusy(true)
    try {
      await api.post('/auth/2fa/disable', { code: code.trim() })
      setDisabling(false)
      setCode('')
      await refresh()
      toast.success(t('twofa.disabled'))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  const codeInput = (
    <Input
      value={code}
      onChange={(e) => setCode(e.target.value)}
      inputMode="numeric"
      autoComplete="one-time-code"
      placeholder={t('twofa.codePlaceholder')}
      className="max-w-48"
    />
  )

  return (
    <Section title={t('twofa.title')} description={t('twofa.description')}>
      {recovery ? (
        <div className="space-y-3">
          <p className="text-sm font-medium">{t('twofa.recoveryHeading')}</p>
          <p className="text-muted-foreground text-xs">{t('twofa.recoveryWarning')}</p>
          <div className="border-border bg-muted/30 grid max-w-md grid-cols-2 gap-2 rounded-lg border p-4 font-mono text-sm">
            {recovery.map((c) => (
              <span key={c}>{c}</span>
            ))}
          </div>
          <Button variant="outline" onClick={() => setRecovery(null)}>
            {t('twofa.recoveryDone')}
          </Button>
        </div>
      ) : user?.two_factor_enabled ? (
        disabling ? (
          <div className="space-y-3">
            <p className="text-muted-foreground text-sm">{t('twofa.disablePrompt')}</p>
            {codeInput}
            <div className="flex gap-2">
              <Button onClick={disable} loading={busy} disabled={!code.trim()}>
                {t('twofa.disable')}
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setDisabling(false)
                  setCode('')
                }}
              >
                {t('common.cancel')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-success text-sm font-medium">{t('twofa.active')}</span>
            <Button variant="outline" onClick={() => setDisabling(true)}>
              {t('twofa.disable')}
            </Button>
          </div>
        )
      ) : setup ? (
        <div className="space-y-3">
          <p className="text-muted-foreground text-sm">{t('twofa.scanPrompt')}</p>
          <img
            src={setup.qr_png}
            alt="QR"
            className="border-border size-44 rounded-lg border bg-white p-2"
          />
          <p className="text-muted-foreground text-xs">
            {t('twofa.manualEntry')}{' '}
            <code className="bg-muted rounded px-1 py-0.5 font-mono break-all">{setup.secret}</code>
          </p>
          {codeInput}
          <div className="flex gap-2">
            <Button onClick={enable} loading={busy} disabled={!code.trim()}>
              {t('twofa.activate')}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setSetup(null)
                setCode('')
              }}
            >
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-muted-foreground text-sm">{t('twofa.inactive')}</span>
          <Button onClick={startSetup} loading={busy}>
            {t('twofa.setup')}
          </Button>
        </div>
      )}
    </Section>
  )
}
