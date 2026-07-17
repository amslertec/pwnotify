import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ShieldMinus, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import type { AdminUser, AdminUsers } from '@/lib/types'

/** Superadmins-Tab (Access-Modell/Superadmin-Phase, Task 6; als Tab extrahiert Task 7):
 *  anlegen (immer lokal), herabstufen/löschen -- der letzte Superadmin ist serverseitig
 *  geschützt (409), die UI spiegelt das defensiv (Buttons deaktiviert), analog zum
 *  Standard-Kunden-Schutz im Kunden-Tab. */
export function SuperadminsTab() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { user: me } = useAuth()
  const [createOpen, setCreateOpen] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.get<AdminUsers>('/admin/users'),
  })

  const superadmins = data?.superadmins ?? []
  const isLastSuperadmin = superadmins.length <= 1

  const demote = useMutation({
    mutationFn: (id: number) =>
      api.post<AdminUser>(`/admin/users/${id}/superadmin`, { promote: false }),
    onSuccess: () => {
      toast.success(t('tenants.superadmins.demoted'))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const del = useMutation({
    mutationFn: (id: number) => api.del<{ message: string }>(`/admin/users/${id}`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          {t('tenants.superadmins.title')}{' '}
          <span className="text-muted-foreground/60">({superadmins.length})</span>
        </h3>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <UserPlus /> {t('tenants.superadmins.create')}
        </Button>
      </div>
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('tenants.superadmins.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.superadmins.colUsername')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                <tr>
                  <td colSpan={3} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : superadmins.length === 0 ? (
                <tr>
                  <td colSpan={3} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.superadmins.empty')}
                  </td>
                </tr>
              ) : (
                superadmins.map((u) => (
                  <tr key={u.id} className="hover:bg-muted/30">
                    <td className="px-4 py-2.5 font-medium">
                      <div className="flex items-center gap-2">
                        {u.display_name || '—'}
                        {u.id === me?.id && <Badge variant="secondary">{t('access.you')}</Badge>}
                      </div>
                    </td>
                    <td className="text-muted-foreground px-4 py-2.5 font-mono text-xs">
                      {u.username}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="icon"
                          disabled={isLastSuperadmin}
                          onClick={() => demote.mutate(u.id)}
                          aria-label={t('tenants.superadmins.demote')}
                          title={
                            isLastSuperadmin
                              ? t('tenants.superadmins.lastHint')
                              : t('tenants.superadmins.demote')
                          }
                        >
                          <ShieldMinus className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          disabled={isLastSuperadmin}
                          onClick={() => del.mutate(u.id)}
                          aria-label={t('common.delete')}
                          title={
                            isLastSuperadmin
                              ? t('tenants.superadmins.lastHint')
                              : t('common.delete')
                          }
                        >
                          <Trash2 className="text-danger size-4" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <CreateSuperadminDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

/** 'password': bestehender Direktanlage-Pfad (Benutzername + Passwort sofort gesetzt).
 *  'invite': Einladungsmodus (Task 10, Parität zu `access.tsx`s `CreateDialog`, Task 5 §7b)
 *  -- nur E-Mail, `POST /admin/users/superadmin` OHNE `password` -> legt ein `pending:`-
 *  Platzhalterkonto (`role=superadmin`) an und verschickt eine Einladungs-Mail; der
 *  Empfänger vergibt Benutzername + Passwort erst beim Annehmen (`/einladung`). */
export type CreateMode = 'password' | 'invite'

interface CreateFormState {
  firstName: string
  lastName: string
  username: string
  password: string
  email: string
}

// Reine Logik ausgelagert (Muster wie `groups-tab.tsx`/`groups-tab.test.ts` -- `vitest.config.ts`
// matched nur `src/**/*.test.ts`, `environment: 'node'`, kein jsdom/`@testing-library/react` im
// Einsatz; das Rendering/Wiring selbst wird über `typecheck`/`build` abgesichert), damit der
// Modus-Zweig (E-Mail-only vs. Benutzername+Passwort) ohne Komponenten-Rendering testbar ist.
export function isSuperadminEmailValid(email: string): boolean {
  return /.+@.+\..+/.test(email.trim())
}

export function canSubmitSuperadminCreate(mode: CreateMode, form: CreateFormState): boolean {
  return mode === 'invite'
    ? isSuperadminEmailValid(form.email)
    : form.username.length >= 3 && form.password.length >= 10
}

export function buildSuperadminCreatePayload(
  mode: CreateMode,
  form: CreateFormState,
): Record<string, unknown> {
  if (mode === 'invite') {
    return { email: form.email.trim() }
  }
  return {
    username: form.username,
    password: form.password,
    display_name: `${form.firstName} ${form.lastName}`.trim() || null,
  }
}

export function superadminCreatedToastKey(mode: CreateMode): string {
  return mode === 'invite' ? 'access.inviteSent' : 'tenants.superadmins.created'
}

function CreateSuperadminDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [mode, setMode] = useState<CreateMode>('password')
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')

  const resetForm = () => {
    setMode('password')
    setFirstName('')
    setLastName('')
    setUsername('')
    setPassword('')
    setEmail('')
  }

  const formState: CreateFormState = { firstName, lastName, username, password, email }

  const create = useMutation({
    mutationFn: () =>
      api.post<AdminUser>('/admin/users/superadmin', buildSuperadminCreatePayload(mode, formState)),
    onSuccess: () => {
      toast.success(t(superadminCreatedToastKey(mode)))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      resetForm()
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const canSubmit = canSubmitSuperadminCreate(mode, formState)

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) resetForm()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.superadmins.createTitle')}</DialogTitle>
        </DialogHeader>
        <Tabs value={mode} onValueChange={(v) => setMode(v as CreateMode)}>
          <TabsList className="w-full">
            <TabsTrigger value="password" className="flex-1">
              {t('access.modeSetPassword')}
            </TabsTrigger>
            <TabsTrigger value="invite" className="flex-1">
              {t('access.modeInvite')}
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="space-y-3">
          {mode === 'password' ? (
            <>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('access.firstName')}</Label>
                  <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label>{t('access.lastName')}</Label>
                  <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
                </div>
              </div>
              <div className="space-y-1.5">
                <Label>{t('access.username')}</Label>
                <Input value={username} onChange={(e) => setUsername(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label>{t('access.passwordLabel')}</Label>
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
            </>
          ) : (
            <div className="space-y-1.5">
              <Label>{t('access.emailLabel')}</Label>
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@example.com"
                autoFocus
              />
              <p className="text-muted-foreground text-xs">{t('access.inviteHint')}</p>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button onClick={() => create.mutate()} loading={create.isPending} disabled={!canSubmit}>
            {mode === 'invite' ? t('access.sendInvite') : t('tenants.superadmins.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
