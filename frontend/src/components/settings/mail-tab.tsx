import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { ConnectionStatus } from '../run-status'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { MASK_MARKER } from '@/lib/constants'
import type { DashboardData } from '@/lib/types'

const STRATEGIES = ['primary', 'alternate', 'both', 'alternate_fallback_primary']

export function MailTab({ settings, save, saving }: SettingsTabProps) {
  const { t } = useTranslation()
  const [backend, setBackend] = useState(String(settings['mail.backend'] ?? 'graph'))
  const [from, setFrom] = useState(String(settings['mail.from'] ?? ''))
  const [strategy, setStrategy] = useState(String(settings['mail.recipient_strategy'] ?? 'primary'))
  const [smtpHost, setSmtpHost] = useState(String(settings['mail.smtp_host'] ?? ''))
  const [smtpPort, setSmtpPort] = useState(String(settings['mail.smtp_port'] ?? 587))
  const [smtpUser, setSmtpUser] = useState(String(settings['mail.smtp_username'] ?? ''))
  const [smtpPass, setSmtpPass] = useState('')
  const [smtpTls, setSmtpTls] = useState(String(settings['mail.smtp_tls'] ?? 'starttls'))
  const [testTo, setTestTo] = useState('')
  const [testing, setTesting] = useState(false)
  const passSet = settings['mail.smtp_password'] === MASK_MARKER
  const { data: dash } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardData>('/dashboard'),
  })

  const onSave = () =>
    save({
      'mail.backend': backend,
      'mail.from': from,
      'mail.recipient_strategy': strategy,
      'mail.smtp_host': smtpHost,
      'mail.smtp_port': Number(smtpPort),
      'mail.smtp_username': smtpUser,
      'mail.smtp_tls': smtpTls,
      ...(smtpPass ? { 'mail.smtp_password': smtpPass } : {}),
    })

  const test = async () => {
    setTesting(true)
    try {
      await onSave()
      await api.post('/settings/mail/test', { to: testTo, locale: 'de' })
      toast.success(t('mailTab.test.sent', { to: testTo }))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setTesting(false)
    }
  }

  return (
    <Section
      title={t('mailTab.title')}
      description={t('mailTab.description')}
      footer={
        <Button onClick={onSave} loading={saving}>
          {t('mailTab.save')}
        </Button>
      }
    >
      <ConnectionStatus
        ok={!!dash?.backends.mail_configured}
        label={
          dash?.backends.mail_configured
            ? t('backendStatus.mail.configured', { backend: dash?.backends.mail_backend })
            : t('backendStatus.mail.notConfigured')
        }
      />
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t('mailTab.fields.backend')}>
          <Select value={backend} onValueChange={setBackend}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="graph">{t('mailTab.backends.graph')}</SelectItem>
              <SelectItem value="smtp">{t('mailTab.backends.smtp')}</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label={t('mailTab.fields.from')}>
          <Input value={from} onChange={(e) => setFrom(e.target.value)} />
        </Field>
        <Field label={t('mailTab.fields.strategy')} className="sm:col-span-2">
          <Select value={strategy} onValueChange={setStrategy}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STRATEGIES.map((s) => (
                <SelectItem key={s} value={s}>
                  {t(`mailTab.strategies.${s}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        {backend === 'smtp' && (
          <>
            <Field label={t('mailTab.fields.smtpHost')}>
              <Input value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
            </Field>
            <Field label={t('mailTab.fields.port')}>
              <Input value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} />
            </Field>
            <Field label={t('mailTab.fields.user')}>
              <Input value={smtpUser} onChange={(e) => setSmtpUser(e.target.value)} />
            </Field>
            <Field
              label={t('mailTab.fields.password')}
              hint={passSet ? t('mailTab.passwordHint') : undefined}
            >
              <Input
                type="password"
                value={smtpPass}
                onChange={(e) => setSmtpPass(e.target.value)}
                placeholder={passSet ? '••••••••' : ''}
              />
            </Field>
            <Field label={t('mailTab.fields.tlsMode')}>
              <Select value={smtpTls} onValueChange={setSmtpTls}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="starttls">{t('mailTab.tls.starttls')}</SelectItem>
                  <SelectItem value="ssl">{t('mailTab.tls.ssl')}</SelectItem>
                  <SelectItem value="none">{t('mailTab.tls.none')}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </>
        )}
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4">
        <p className="mb-2 text-sm font-medium">{t('mailTab.test.title')}</p>
        <div className="flex gap-2">
          <Input
            value={testTo}
            onChange={(e) => setTestTo(e.target.value)}
            placeholder={t('mailTab.test.placeholder')}
          />
          <Button variant="outline" onClick={test} loading={testing} disabled={!testTo || !from}>
            {t('mailTab.test.send')}
          </Button>
        </div>
      </div>
    </Section>
  )
}
