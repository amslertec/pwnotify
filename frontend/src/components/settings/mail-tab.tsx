import { useState } from 'react'
import { toast } from 'sonner'

import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Field, Section } from './section'
import type { SettingsTabProps } from '@/pages/settings'
import { api, ApiError } from '@/lib/api'
import { MASK_MARKER } from '@/lib/constants'

const STRATEGIES = [
  { value: 'primary', label: 'Nur primäre Mailbox' },
  { value: 'alternate', label: 'Nur Alternativadresse' },
  { value: 'both', label: 'Beide (primär + alternativ)' },
  { value: 'alternate_fallback_primary', label: 'Alternativ, sonst primär' },
]

export function MailTab({ settings, save, saving }: SettingsTabProps) {
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
      toast.success(`Test-Mail an ${testTo} gesendet`)
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Versand fehlgeschlagen')
    } finally {
      setTesting(false)
    }
  }

  return (
    <Section
      title="E-Mail-Versand"
      description="Backend, Absender und Empfänger-Strategie."
      footer={
        <Button onClick={onSave} loading={saving}>
          Speichern
        </Button>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Backend">
          <Select value={backend} onValueChange={setBackend}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="graph">Microsoft Graph</SelectItem>
              <SelectItem value="smtp">SMTP</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label="Absenderadresse">
          <Input value={from} onChange={(e) => setFrom(e.target.value)} />
        </Field>
        <Field label="Empfänger-Strategie" className="sm:col-span-2">
          <Select value={strategy} onValueChange={setStrategy}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STRATEGIES.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
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
            <Field
              label="Passwort"
              hint={passSet ? 'Gesetzt — leer lassen zum Beibehalten.' : undefined}
            >
              <Input
                type="password"
                value={smtpPass}
                onChange={(e) => setSmtpPass(e.target.value)}
                placeholder={passSet ? '••••••••' : ''}
              />
            </Field>
            <Field label="TLS-Modus">
              <Select value={smtpTls} onValueChange={setSmtpTls}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="starttls">STARTTLS</SelectItem>
                  <SelectItem value="ssl">SSL/TLS</SelectItem>
                  <SelectItem value="none">Keine</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </>
        )}
      </div>

      <div className="border-border bg-muted/40 rounded-lg border p-4">
        <p className="mb-2 text-sm font-medium">Test-Mail senden</p>
        <div className="flex gap-2">
          <Input
            value={testTo}
            onChange={(e) => setTestTo(e.target.value)}
            placeholder="empfaenger@example.com"
          />
          <Button variant="outline" onClick={test} loading={testing} disabled={!testTo || !from}>
            Senden
          </Button>
        </div>
      </div>
    </Section>
  )
}
