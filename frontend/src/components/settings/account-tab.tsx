import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogOut, Monitor, Trash2, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { UserAvatar } from '../user-avatar'
import { Button } from '../ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../ui/card'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import { LockableInput } from './lockable-input'
import { TwoFactorSection } from './two-factor-section'
import { api, uploadFile } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtRelative } from '@/lib/format'
import type { Session } from '@/lib/types'

export function AccountTab() {
  const isSso = !!useAuth().user?.is_sso

  return (
    <div className="space-y-4">
      <IdentityCard />
      {!isSso && (
        <div className="grid gap-4 lg:grid-cols-2">
          <PasswordCard />
          <TwoFactorSection />
        </div>
      )}
      <SessionsCard />
    </div>
  )
}

/** Avatar + Name in einer Karte. SSO: Avatar aus Entra, Name nur Anzeige. */
function IdentityCard() {
  const { t } = useTranslation()
  const { user, refresh } = useAuth()
  const isSso = !!user?.is_sso

  const nameParts = (user?.display_name ?? '').split(' ')
  const [firstName, setFirstName] = useState(nameParts[0] ?? '')
  const [lastName, setLastName] = useState(nameParts.slice(1).join(' '))
  const [nameBusy, setNameBusy] = useState(false)
  const [lockSignal, setLockSignal] = useState(0)
  const hadName = !!user?.display_name

  const avatarRef = useRef<HTMLInputElement>(null)
  const uploadAvatar = async (file?: File) => {
    if (!file) return
    try {
      await uploadFile('/auth/me/avatar', file)
      await refresh()
      toast.success(t('account.avatarUpdated'))
    } catch (e) {
      toast.error(translateError(e))
    }
  }
  const removeAvatar = async () => {
    try {
      await api.del('/auth/me/avatar')
      await refresh()
      toast.success(t('account.avatarRemoved'))
    } catch (e) {
      toast.error(translateError(e))
    }
  }

  const saveName = async () => {
    setNameBusy(true)
    try {
      await api.post('/auth/profile', { display_name: `${firstName} ${lastName}`.trim() || null })
      await refresh()
      setLockSignal((n) => n + 1)
      toast.success(t('account.nameSaved'))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setNameBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('account.profileTitle')}</CardTitle>
        <CardDescription>
          {isSso ? t('account.profileDescSso') : t('account.profileDesc')}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-6 sm:flex-row sm:items-stretch">
          {/* Avatar + Upload-Aktionen — abgesetzt durch eigenen Rahmen und eine Trennlinie
              zum Namensbereich. */}
          <div className="border-border bg-muted/30 flex flex-col items-center gap-3 rounded-xl border p-5 sm:w-52">
            <UserAvatar className="size-24 text-3xl" />
            {!isSso && (
              <div className="flex w-full flex-col gap-2">
                <input
                  ref={avatarRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(e) => uploadAvatar(e.target.files?.[0])}
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full"
                  onClick={() => avatarRef.current?.click()}
                >
                  <Upload /> {t('account.uploadImage')}
                </Button>
                {user?.has_avatar && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full"
                    onClick={removeAvatar}
                  >
                    <Trash2 className="text-danger size-4" /> {t('account.remove')}
                  </Button>
                )}
              </div>
            )}
          </div>

          {/* Name */}
          <div className="flex-1">
            {isSso ? (
              <div>
                <p className="text-muted-foreground mb-1 text-xs font-medium">
                  {t('account.nameTitle')}
                </p>
                <p className="text-lg font-medium">{user?.display_name || user?.username}</p>
                <p className="text-muted-foreground mt-1 text-sm">{user?.username}</p>
              </div>
            ) : (
              <>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label={t('account.firstName')}>
                    <LockableInput
                      value={firstName}
                      onChange={setFirstName}
                      hasSavedValue={hadName}
                      lockSignal={lockSignal}
                      canUnlock
                    />
                  </Field>
                  <Field label={t('account.lastName')}>
                    <LockableInput
                      value={lastName}
                      onChange={setLastName}
                      hasSavedValue={hadName}
                      lockSignal={lockSignal}
                      canUnlock
                    />
                  </Field>
                </div>
                <div className="mt-4 flex justify-end">
                  <Button onClick={saveName} loading={nameBusy}>
                    {t('account.save')}
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/** Passwort ändern (nur lokale Konten). */
function PasswordCard() {
  const { t } = useTranslation()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  const change = async () => {
    if (next.length < 10) return toast.error(t('account.pwTooShort'))
    if (next !== confirm) return toast.error(t('account.pwMismatch'))
    setBusy(true)
    try {
      await api.post('/auth/password', { current_password: current, new_password: next })
      toast.success(t('account.pwChanged'))
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section
      title={t('account.pwTitle')}
      description={t('account.pwDesc')}
      footer={
        <Button onClick={change} loading={busy} disabled={!current || !next}>
          {t('account.pwTitle')}
        </Button>
      }
    >
      <div className="grid gap-4">
        <Field label={t('account.currentPassword')}>
          <Input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <Field label={t('account.newPassword')}>
          <Input type="password" value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        <Field label={t('account.confirmPassword')}>
          <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
      </div>
    </Section>
  )
}

/** Aktive Sitzungen (Geräte). */
function SessionsCard() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { data: sessions } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<Session[]>('/auth/sessions'),
  })
  const revokeOthers = useMutation({
    mutationFn: () => api.post<{ message: string }>('/auth/sessions/revoke-others'),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['sessions'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Section
      title={t('account.sessionsTitle')}
      description={t('account.sessionsDesc')}
      footer={
        (sessions?.length ?? 0) > 1 ? (
          <Button
            variant="outline"
            onClick={() => revokeOthers.mutate()}
            loading={revokeOthers.isPending}
          >
            <LogOut /> {t('account.revokeOthers')}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-2">
        {sessions?.map((s) => (
          <div
            key={s.id}
            className="border-border flex items-center gap-3 rounded-lg border px-4 py-3 text-sm"
          >
            <Monitor className="text-muted-foreground size-4" />
            <div className="min-w-0 flex-1">
              <p className="truncate">{s.user_agent ?? t('account.unknownDevice')}</p>
              <p className="text-muted-foreground text-xs">
                {s.ip_address ?? '—'} · {t('account.activeLabel')} {fmtRelative(s.last_used_at)}
              </p>
            </div>
            {s.current && (
              <span className="bg-success/10 text-success rounded-full px-2 py-0.5 text-xs font-medium">
                {t('account.currentSession')}
              </span>
            )}
          </div>
        ))}
      </div>
    </Section>
  )
}
