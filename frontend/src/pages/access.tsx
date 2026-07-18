import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Crown, KeyRound, RefreshCw, Send, ShieldCheck, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { AvatarImage } from '@/components/avatar-image'
import { PageHeader } from '@/components/page-header'
import { StatusDot } from '@/components/status-badge'
import { SuperadminsTab } from '@/components/tenants/superadmins-tab'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api } from '@/lib/api'
import { hasAdminRights, isDefaultContext, useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtDate, fmtRelative } from '@/lib/format'
import type { AdminUser, AdminUsers } from '@/lib/types'

const byRole = (list: AdminUser[] | undefined, role: string) =>
  (list ?? []).filter((u) => u.role === role)

/** Bild-Quelle für `AvatarImage` (Task B) -- dieselbe Logik wie `UserAvatar`s eigenes Profilbild
 *  (`components/user-avatar.tsx`), aber admin-facing über die neue `/admin/users/{id}/avatar`-
 *  Route statt `/auth/me/avatar`. `undefined` ohne `has_avatar` -> `AvatarImage` fällt selbst auf
 *  die Initialen zurück, kein zusätzliches Gating hier nötig. Als eigene Funktion exportiert,
 *  damit sie ohne DOM/JSX getestet werden kann (Muster wie `resetGate`/`resolveAvatarView`). */
export function adminAvatarSrc(u: Pick<AdminUser, 'id' | 'has_avatar' | 'avatar_version'>) {
  return u.has_avatar ? `/api/admin/users/${u.id}/avatar?v=${u.avatar_version}` : undefined
}

/** Gate für den Passwort-Reset-Button (Task 6): der Server lehnt einen Reset für ein Konto
 *  ohne E-Mail ohnehin mit `email_required` ab -- dies ist die proaktive UX-Spiegelung davon,
 *  damit der Button erst gar nicht anklickbar aussieht. Ein pending-Konto (`!is_active`) hat
 *  weiterhin Vorrang vor dem E-Mail-Grund (kein Passwort zum Zurücksetzen vorhanden). SSO-
 *  Konten werden NIE auf die lokale E-Mail-Regel gegated -- ihre E-Mail kommt aus Entra und
 *  ihr Reset läuft ohnehin nicht über diesen Button (Route zeigt ihn nur für `!sso`). */
export type ResetGate = { disabled: boolean; hint: 'pending' | 'noEmail' | null }

export function resetGate(u: Pick<AdminUser, 'is_active' | 'is_sso' | 'email'>): ResetGate {
  if (!u.is_active) return { disabled: true, hint: 'pending' }
  if (!u.is_sso && !u.email) return { disabled: true, hint: 'noEmail' }
  return { disabled: false, hint: null }
}

/** Ob der "SSO-Benutzer"-Tab auf der Access-Seite angezeigt wird (Task 6). In Multi-Tenant-Modus
 *  werden SSO-Mitglieder über die Teams auf der Kunden-Seite verwaltet -- der instanzweite
 *  SSO-Tab wäre dort redundant/irreführend. Reine Funktion, damit sie ohne Rendering testbar ist
 *  (Muster wie `isSwitcherVisible`). */
export function showSsoTab(multiTenant: boolean): boolean {
  return !multiTenant
}

export default function AccessPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { user: me } = useAuth()
  const isAdmin = hasAdminRights(me?.role)
  // Superadmin-Verwaltung erscheint NUR für einen Superadmin im Default-Kontext (Provider-Ebene) --
  // dieselbe Gate-Regel, unter der die Tenant-Konsole diese Tabelle früher zeigte. `isDefaultContext`
  // impliziert bereits `role==='superadmin'`; ein Kunden-Kontext (oder jeder Nicht-Superadmin) blendet
  // den Tab aus, und der Server liefert dort ohnehin keinen `superadmins`-Schlüssel.
  const showSuperadmins = isDefaultContext(me)
  const multiTenant = me?.multi_tenant_mode ?? false
  const [createOpen, setCreateOpen] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.get<AdminUsers>('/admin/users'),
  })

  const total = (data?.local.length ?? 0) + (data?.sso.length ?? 0)
  // Aktive Administratoren (lokal + SSO). Der letzte darf nicht herabgestuft werden.
  const adminCount =
    byRole(data?.local, 'admin').length + byRole(data?.sso, 'admin').length

  const del = useMutation({
    mutationFn: (id: number) => api.del<{ message: string }>(`/admin/users/${id}`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const sync = useMutation({
    mutationFn: () => api.post<{ message: string }>('/admin/users/sso/sync'),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <div>
      <PageHeader title={t('access.title')} description={t('access.description')} />

      <Tabs defaultValue="local">
        <TabsList>
          <TabsTrigger value="local">
            <KeyRound className="size-4" /> {t('access.tabLocal')}
          </TabsTrigger>
          {showSsoTab(multiTenant) && (
            <TabsTrigger value="sso">
              <ShieldCheck className="size-4" /> {t('access.tabSso')}
            </TabsTrigger>
          )}
          {showSuperadmins && (
            <TabsTrigger value="superadmins">
              <Crown className="size-4" /> {t('access.tabSuperadmins')}
            </TabsTrigger>
          )}
        </TabsList>

        {/* Lokale Benutzer — je Rolle eine Tabelle */}
        <TabsContent value="local">
          {isAdmin && (
            <div className="mb-3 flex justify-end">
              <Button onClick={() => setCreateOpen(true)}>
                <UserPlus /> {t('access.newLocalUser')}
              </Button>
            </div>
          )}
          <div className="space-y-6">
            <RoleSection
              titleKey="access.roleAdmins"
              users={byRole(data?.local, 'admin')}
              isLoading={isLoading}
              isAdmin={isAdmin}
              meId={me?.id}
              total={total}
              adminCount={adminCount}
              onDelete={(id) => del.mutate(id)}
            />
            <RoleSection
              titleKey="access.roleAuditors"
              users={byRole(data?.local, 'auditor')}
              isLoading={isLoading}
              isAdmin={isAdmin}
              meId={me?.id}
              total={total}
              adminCount={adminCount}
              onDelete={(id) => del.mutate(id)}
            />
          </div>
        </TabsContent>

        {/* SSO-Benutzer — je Rolle eine Tabelle */}
        {showSsoTab(multiTenant) && (
          <TabsContent value="sso">
            {isAdmin && (
              <div className="mb-3 flex justify-end">
                <Button variant="outline" onClick={() => sync.mutate()} loading={sync.isPending}>
                  <RefreshCw className="size-3.5" /> {t('access.syncEntra')}
                </Button>
              </div>
            )}
            {!isLoading && (data?.sso.length ?? 0) === 0 ? (
              <Card className="overflow-hidden">
                <p className="text-muted-foreground px-4 py-8 text-center text-sm">
                  {t('access.noSsoUsers')}
                </p>
              </Card>
            ) : (
              <div className="space-y-6">
                <RoleSection
                  titleKey="access.roleAdmins"
                  users={byRole(data?.sso, 'admin')}
                  sso
                  isLoading={isLoading}
                  isAdmin={isAdmin}
                  meId={me?.id}
                  total={total}
                  adminCount={adminCount}
                  onDelete={(id) => del.mutate(id)}
                />
                <RoleSection
                  titleKey="access.roleAuditors"
                  users={byRole(data?.sso, 'auditor')}
                  sso
                  isLoading={isLoading}
                  isAdmin={isAdmin}
                  meId={me?.id}
                  total={total}
                  adminCount={adminCount}
                  onDelete={(id) => del.mutate(id)}
                />
              </div>
            )}
          </TabsContent>
        )}

        {showSuperadmins && (
          <TabsContent value="superadmins">
            <SuperadminsTab />
          </TabsContent>
        )}
      </Tabs>

      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

function RoleSection({
  titleKey,
  users,
  sso,
  isLoading,
  isAdmin,
  meId,
  total,
  adminCount,
  onDelete,
}: {
  titleKey: string
  users: AdminUser[]
  sso?: boolean
  isLoading: boolean
  isAdmin: boolean
  meId: number | undefined
  total: number
  adminCount: number
  onDelete: (id: number) => void
}) {
  const { t } = useTranslation()
  return (
    <div>
      <h3 className="text-muted-foreground mb-2 text-xs font-semibold tracking-wide uppercase">
        {t(titleKey)} <span className="text-muted-foreground/60">({users.length})</span>
      </h3>
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('access.colName')}</th>
                <th className="px-4 py-3 font-medium">
                  {sso ? t('access.colUpn') : t('access.colUsername')}
                </th>
                <th className="px-4 py-3 font-medium">{t('access.colEmail')}</th>
                {sso && <th className="px-4 py-3 font-medium">{t('access.colStatus')}</th>}
                <th className="px-4 py-3 font-medium">{t('access.colLastLogin')}</th>
                {!sso && <th className="px-4 py-3 font-medium">{t('access.colCreated')}</th>}
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                <tr>
                  <td colSpan={6} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : users.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-muted-foreground px-4 py-6 text-center">
                    {t('access.emptyRole')}
                  </td>
                </tr>
              ) : (
                users.map((u) => (
                  <UserRow
                    key={u.id}
                    u={u}
                    sso={sso}
                    isAdmin={isAdmin}
                    isSelf={u.id === meId}
                    canDelete={isAdmin && total > 1 && u.id !== meId}
                    // Letzten Admin nicht herabstufbar machen (Aussperr-Schutz).
                    isLastAdmin={u.role === 'admin' && adminCount <= 1}
                    onDelete={() => onDelete(u.id)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function UserRow({
  u,
  sso,
  isAdmin,
  isSelf,
  canDelete,
  isLastAdmin,
  onDelete,
}: {
  u: AdminUser
  sso?: boolean
  isAdmin: boolean
  isSelf: boolean
  canDelete: boolean
  isLastAdmin: boolean
  onDelete: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const roleMut = useMutation({
    mutationFn: (role: string) =>
      api.post<{ message: string }>(`/admin/users/${u.id}/role`, { role }),
    onSuccess: () => {
      toast.success(t('access.roleChanged'))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  // Passwort-Reset-Link auslösen (§7c) -- nur für lokale, bereits aktivierte Konten
  // sinnvoll (SSO setzt sein Passwort über Entra zurück; ein noch nicht angenommenes
  // `pending:`-Einladungskonto hat gar kein Passwort, das man zurücksetzen könnte). Der
  // Server lehnt ein Konto ohne E-Mail zusätzlich mit `email_required` ab -- `resetGate`
  // (Task 6) spiegelt das proaktiv in der UI, statt erst auf den Server-Fehler zu warten.
  const gate = resetGate(u)
  const resetMut = useMutation({
    mutationFn: () => api.post<{ message: string }>(`/admin/users/${u.id}/reset`),
    onSuccess: (r) => toast.success(r.message),
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <AvatarImage
            name={u.display_name || u.username}
            src={adminAvatarSrc(u)}
            className="size-8 shrink-0 text-xs"
          />
          <span className="font-medium">{u.display_name || '—'}</span>
          {isSelf && <Badge variant="secondary">{t('access.you')}</Badge>}
        </div>
      </td>
      <td className="text-muted-foreground max-w-[260px] truncate px-4 py-2.5 font-mono text-xs">
        {u.username}
      </td>
      <td className="text-muted-foreground max-w-[260px] truncate px-4 py-2.5 text-xs">
        {u.email ?? '—'}
      </td>
      {sso && (
        <td className="px-4 py-2.5">
          <span className="inline-flex items-center gap-1.5 text-xs">
            <StatusDot status={u.is_active ? 'ok' : 'disabled'} />
            {u.is_active ? t('access.statusActive') : t('access.statusDisabled')}
          </span>
        </td>
      )}
      <td className="text-muted-foreground px-4 py-2.5">{fmtRelative(u.last_login_at)}</td>
      {!sso && <td className="text-muted-foreground px-4 py-2.5">{fmtDate(u.created_at)}</td>}
      <td className="px-4 py-2.5">
        <div className="flex items-center justify-end gap-2">
          {!sso && isAdmin && (
            <Select
              value={u.role}
              onValueChange={(role) => roleMut.mutate(role)}
              disabled={roleMut.isPending}
            >
              <SelectTrigger className="h-8 w-36" aria-label={t('access.changeRole')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="admin">{t('access.roleAdmin')}</SelectItem>
                {/* Letzten Admin nicht auf Auditor herabstufbar — sonst Aussperrung. */}
                <SelectItem value="auditor" disabled={isLastAdmin}>
                  {t('access.roleAuditor')}
                  {isLastAdmin && ` — ${t('access.lastAdminHint')}`}
                </SelectItem>
              </SelectContent>
            </Select>
          )}
          {!sso && isAdmin && (
            <Button
              variant="ghost"
              size="icon"
              disabled={gate.disabled || resetMut.isPending}
              onClick={() => resetMut.mutate()}
              aria-label={t('access.sendReset')}
              title={
                gate.hint === 'pending'
                  ? t('access.resetPendingHint')
                  : gate.hint === 'noEmail'
                    ? t('access.resetNoEmailHint')
                    : t('access.sendReset')
              }
            >
              <Send className="size-4" />
            </Button>
          )}
          {isAdmin && (
            <Button
              variant="ghost"
              size="icon"
              disabled={!canDelete}
              onClick={onDelete}
              aria-label={t('access.delete')}
              title={!canDelete ? t('access.cannotDelete') : t('access.delete')}
            >
              <Trash2 className="text-danger size-4" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  )
}

/** 'password': bestehender Direktanlage-Pfad (Benutzername + Passwort sofort gesetzt).
 *  'invite': Einladungsmodus (Task 5 §7b) -- nur E-Mail + Rolle, `POST /admin/users` OHNE
 *  `password` -> legt ein `pending:`-Platzhalterkonto an und verschickt eine Einladungs-Mail;
 *  der Einladungsempfänger vergibt Benutzername + Passwort erst beim Annehmen (`/einladung`). */
type CreateMode = 'password' | 'invite'

function CreateDialog({
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
  const [role, setRole] = useState('admin')

  const resetForm = () => {
    setMode('password')
    setFirstName('')
    setLastName('')
    setUsername('')
    setPassword('')
    setEmail('')
    setRole('admin')
  }

  const create = useMutation({
    mutationFn: () =>
      mode === 'invite'
        ? api.post('/admin/users', { email: email.trim(), role })
        : api.post('/admin/users', {
            username,
            password,
            role,
            display_name: `${firstName} ${lastName}`.trim() || null,
          }),
    onSuccess: () => {
      toast.success(mode === 'invite' ? t('access.inviteSent') : t('access.userCreated'))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      resetForm()
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const emailValid = /.+@.+\..+/.test(email.trim())
  const canSubmit = mode === 'invite' ? emailValid : username.length >= 3 && password.length >= 10

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
          <DialogTitle>{t('access.createTitle')}</DialogTitle>
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
          <div className="space-y-1.5">
            <Label>{t('access.roleLabel')}</Label>
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="admin">{t('access.roleAdmin')}</SelectItem>
                <SelectItem value="auditor">{t('access.roleAuditor')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter>
          <Button onClick={() => create.mutate()} loading={create.isPending} disabled={!canSubmit}>
            {mode === 'invite' ? t('access.sendInvite') : t('access.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
