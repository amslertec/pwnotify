import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogOut, Monitor, Trash2, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { UserAvatar } from '../user-avatar'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import { TwoFactorSection } from './two-factor-section'
import { api, uploadFile } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtRelative } from '@/lib/format'
import type { Session } from '@/lib/types'

export function AccountTab() {
  const { t } = useTranslation()
  const { user, refresh } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  const nameParts = (user?.display_name ?? '').split(' ')
  const [firstName, setFirstName] = useState(nameParts[0] ?? '')
  const [lastName, setLastName] = useState(nameParts.slice(1).join(' '))
  const [nameBusy, setNameBusy] = useState(false)

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
      toast.success(t('account.nameSaved'))
    } catch (e) {
      toast.error(translateError(e))
    } finally {
      setNameBusy(false)
    }
  }

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
    <div className="space-y-4">
      <Section
        title={t('account.avatarTitle')}
        description={
          user?.is_sso ? t('account.avatarDescSso') : t('account.avatarDesc')
        }
      >
        <div className="flex items-center gap-4">
          <UserAvatar className="size-16 text-xl" />
          {!user?.is_sso && (
            <div className="flex flex-wrap gap-2">
              <input
                ref={avatarRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={(e) => uploadAvatar(e.target.files?.[0])}
              />
              <Button variant="outline" size="sm" onClick={() => avatarRef.current?.click()}>
                <Upload /> {t('account.uploadImage')}
              </Button>
              {user?.has_avatar && (
                <Button variant="outline" size="sm" onClick={removeAvatar}>
                  <Trash2 className="text-danger size-4" /> {t('account.remove')}
                </Button>
              )}
            </div>
          )}
        </div>
      </Section>

      {!user?.is_sso && (
        <Section
          title={t('account.nameTitle')}
          description={t('account.nameDesc')}
          footer={
            <Button onClick={saveName} loading={nameBusy}>
              {t('account.save')}
            </Button>
          }
        >
          <div className="grid max-w-md grid-cols-2 gap-4">
            <Field label={t('account.firstName')}>
              <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </Field>
            <Field label={t('account.lastName')}>
              <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </Field>
          </div>
        </Section>
      )}

      {!user?.is_sso && (
        <Section
          title={t('account.pwTitle')}
          description={t('account.pwDesc')}
          footer={
            <Button onClick={change} loading={busy} disabled={!current || !next}>
              {t('account.pwTitle')}
            </Button>
          }
        >
          <div className="grid max-w-md gap-4">
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
      )}

      <TwoFactorSection />

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
    </div>
  )
}
