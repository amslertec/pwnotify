import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogOut, Monitor, Trash2, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'

import { UserAvatar } from '../user-avatar'
import { Button } from '../ui/button'
import { Input } from '../ui/input'
import { Field, Section } from './section'
import { api, ApiError, uploadFile } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { fmtRelative } from '@/lib/format'
import type { Session } from '@/lib/types'

export function AccountTab() {
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
      toast.success('Profilbild aktualisiert')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Upload fehlgeschlagen')
    }
  }
  const removeAvatar = async () => {
    try {
      await api.del('/auth/me/avatar')
      await refresh()
      toast.success('Profilbild entfernt')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Fehler')
    }
  }

  const saveName = async () => {
    setNameBusy(true)
    try {
      await api.post('/auth/profile', { display_name: `${firstName} ${lastName}`.trim() || null })
      await refresh()
      toast.success('Name gespeichert')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Fehler')
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
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  const change = async () => {
    if (next.length < 10) return toast.error('Neues Passwort mind. 10 Zeichen')
    if (next !== confirm) return toast.error('Passwörter stimmen nicht überein')
    setBusy(true)
    try {
      await api.post('/auth/password', { current_password: current, new_password: next })
      toast.success('Passwort geändert')
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Fehler')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <Section
        title="Profilbild"
        description={
          user?.is_sso
            ? 'Wird automatisch aus Microsoft Entra übernommen.'
            : 'Angezeigt oben rechts und in Ihrem Konto.'
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
                <Upload /> Bild hochladen
              </Button>
              {user?.has_avatar && (
                <Button variant="outline" size="sm" onClick={removeAvatar}>
                  <Trash2 className="text-danger size-4" /> Entfernen
                </Button>
              )}
            </div>
          )}
        </div>
      </Section>

      {!user?.is_sso && (
        <Section
          title="Name"
          description="Ihr angezeigter Name in der App."
          footer={
            <Button onClick={saveName} loading={nameBusy}>
              Speichern
            </Button>
          }
        >
          <div className="grid max-w-md grid-cols-2 gap-4">
            <Field label="Vorname">
              <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </Field>
            <Field label="Nachname">
              <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </Field>
          </div>
        </Section>
      )}

      {!user?.is_sso && (
        <Section
          title="Passwort ändern"
          description="Für Ihr PwNotify-Anmeldekonto."
          footer={
            <Button onClick={change} loading={busy} disabled={!current || !next}>
              Passwort ändern
            </Button>
          }
        >
          <div className="grid max-w-md gap-4">
            <Field label="Aktuelles Passwort">
              <Input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </Field>
            <Field label="Neues Passwort (mind. 10 Zeichen)">
              <Input type="password" value={next} onChange={(e) => setNext(e.target.value)} />
            </Field>
            <Field label="Bestätigen">
              <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </Field>
          </div>
        </Section>
      )}

      <Section
        title="Aktive Sitzungen"
        description="Angemeldete Geräte dieses Kontos."
        footer={
          (sessions?.length ?? 0) > 1 ? (
            <Button
              variant="outline"
              onClick={() => revokeOthers.mutate()}
              loading={revokeOthers.isPending}
            >
              <LogOut /> Andere Sitzungen abmelden
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
                <p className="truncate">{s.user_agent ?? 'Unbekanntes Gerät'}</p>
                <p className="text-muted-foreground text-xs">
                  {s.ip_address ?? '—'} · aktiv {fmtRelative(s.last_used_at)}
                </p>
              </div>
              {s.current && (
                <span className="bg-success/10 text-success rounded-full px-2 py-0.5 text-xs font-medium">
                  Diese Sitzung
                </span>
              )}
            </div>
          ))}
        </div>
      </Section>
    </div>
  )
}
