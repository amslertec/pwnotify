import { ShieldCheck, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { useAuth } from '@/lib/auth'
import type { TwoFactorSetup } from '@/lib/types'

/** 2FA-Verwaltung als Status-Karte (nur lokale Konten). Zeigt den Zustand deutlich über
 *  ein Schild-Icon und führt durch Einrichtung bzw. Deaktivierung. */
export function TwoFactorSection() {
  const { t } = useTranslation()
  const { user, refresh } = useAuth()
  const [setup, setSetup] = useState<TwoFactorSetup | null>(null)
  const [recovery, setRecovery] = useState<string[] | null>(null)
  const [disabling, setDisabling] = useState(false)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)

  if (user?.is_sso) return null // SSO-Konten nutzen die Entra-Anmeldung

  const active = !!user?.two_factor_enabled

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
    <div className="border-border bg-card flex h-full flex-col items-center rounded-xl border p-6 text-center">
      {/* Schild zentriert und gross — grün = aktiv, rot = nicht aktiv. Die Farbe sagt den
          Zustand, deshalb braucht es bei aktiv keine zusätzliche „Aktiv"-Zeile. */}
      <div
        className="flex size-20 items-center justify-center rounded-full"
        style={{
          background: active
            ? 'color-mix(in srgb, var(--status-ok) 14%, transparent)'
            : 'color-mix(in srgb, var(--status-expired) 14%, transparent)',
        }}
      >
        {active ? (
          <ShieldCheck className="size-10" style={{ color: 'var(--status-ok)' }} />
        ) : (
          <ShieldAlert className="size-10" style={{ color: 'var(--status-expired)' }} />
        )}
      </div>
      <h3 className="font-display mt-4 text-base font-semibold">{t('twofa.title')}</h3>
      {!active && (
        <p className="text-muted-foreground mt-1 max-w-xs text-sm">{t('twofa.description')}</p>
      )}

      <div className="mt-5 flex w-full flex-1 flex-col items-center">
        {recovery ? (
          <div className="space-y-3">
            <p className="text-sm font-medium">{t('twofa.recoveryHeading')}</p>
            <p className="text-muted-foreground text-xs">{t('twofa.recoveryWarning')}</p>
            <div className="border-border bg-muted/30 grid grid-cols-2 gap-2 rounded-lg border p-4 font-mono text-sm">
              {recovery.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </div>
            <Button variant="outline" size="sm" onClick={() => setRecovery(null)}>
              {t('twofa.recoveryDone')}
            </Button>
          </div>
        ) : active ? (
          disabling ? (
            <div className="flex flex-col items-center gap-3">
              <p className="text-muted-foreground text-sm">{t('twofa.disablePrompt')}</p>
              {codeInput}
              <div className="flex gap-2">
                <Button
                  variant="destructive"
                  onClick={disable}
                  loading={busy}
                  disabled={!code.trim()}
                >
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
            <Button variant="destructive" size="sm" onClick={() => setDisabling(true)}>
              {t('twofa.disable')}
            </Button>
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
              <code className="bg-muted rounded px-1 py-0.5 font-mono break-all">
                {setup.secret}
              </code>
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
          <Button onClick={startSetup} loading={busy}>
            {t('twofa.setup')}
          </Button>
        )}
      </div>
    </div>
  )
}
