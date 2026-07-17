import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, ShieldMinus, Trash2, UserPlus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { PageHeader } from '@/components/page-header'
import { StatusDot } from '@/components/status-badge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
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
import { Switch } from '@/components/ui/switch'
import { api } from '@/lib/api'
import { hasAdminRights, useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtDate } from '@/lib/format'
import type { AdminUser, AdminUsers, Assignment, Tenant } from '@/lib/types'

const SLUG_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/

export default function TenantsPage() {
  const { t } = useTranslation()
  const { user: me } = useAuth()
  const isAdmin = hasAdminRights(me?.role)
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<Tenant | null>(null)
  const [deleting, setDeleting] = useState<Tenant | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: () => api.get<Tenant[]>('/admin/tenants'),
  })

  const tenants = data ?? []

  return (
    <div>
      <PageHeader
        title={t('tenants.title')}
        description={t('tenants.description')}
        actions={
          isAdmin && (
            <Button onClick={() => setCreateOpen(true)}>
              <Plus /> {t('tenants.new')}
            </Button>
          )
        }
      />

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('tenants.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.colSlug')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.colEntraTenantId')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.colActive')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.colSsoUsers')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.colCreated')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                <tr>
                  <td colSpan={7} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : tenants.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.empty')}
                  </td>
                </tr>
              ) : (
                tenants.map((tn) => (
                  <TenantRow
                    key={tn.id}
                    tenant={tn}
                    isAdmin={isAdmin}
                    onEdit={() => setEditing(tn)}
                    onDelete={() => setDeleting(tn)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      {editing && (
        <EditDialog
          key={editing.id}
          tenant={editing}
          onOpenChange={(o) => !o && setEditing(null)}
        />
      )}
      {deleting && (
        <DeleteDialog
          key={deleting.id}
          tenant={deleting}
          onOpenChange={(o) => !o && setDeleting(null)}
        />
      )}

      <AssignmentsSection tenants={tenants} />
      <SuperadminsSection />
    </div>
  )
}

function TenantRow({
  tenant,
  isAdmin,
  onEdit,
  onDelete,
}: {
  tenant: Tenant
  isAdmin: boolean
  onEdit: () => void
  onDelete: () => void
}) {
  const { t } = useTranslation()
  // Der Standard-Kunde (Migrations-Anker) darf serverseitig weder gelöscht noch
  // deaktiviert werden -- die UI bietet die Aktion daher gar nicht erst an.
  const isDefault = tenant.slug === 'default'

  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2.5 font-medium">{tenant.name}</td>
      <td className="text-muted-foreground px-4 py-2.5 font-mono text-xs">{tenant.slug}</td>
      <td className="text-muted-foreground px-4 py-2.5 font-mono text-xs">
        {tenant.entra_tenant_id ?? '—'}
      </td>
      <td className="px-4 py-2.5">
        <Badge variant={tenant.is_active ? 'success' : 'secondary'}>
          <StatusDot status={tenant.is_active ? 'ok' : 'disabled'} />
          {tenant.is_active ? t('tenants.statusActive') : t('tenants.statusInactive')}
        </Badge>
      </td>
      <td className="text-muted-foreground px-4 py-2.5">{tenant.sso_user_count}</td>
      <td className="text-muted-foreground px-4 py-2.5">{fmtDate(tenant.created_at)}</td>
      <td className="px-4 py-2.5">
        <div className="flex items-center justify-end gap-2">
          {isAdmin && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onEdit}
              aria-label={t('common.edit')}
              title={t('common.edit')}
            >
              <Pencil className="size-4" />
            </Button>
          )}
          {isAdmin && (
            <Button
              variant="ghost"
              size="icon"
              disabled={isDefault}
              onClick={onDelete}
              aria-label={t('common.delete')}
              title={isDefault ? t('tenants.defaultProtected') : t('common.delete')}
            >
              <Trash2 className="text-danger size-4" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  )
}

function CreateDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { refresh } = useAuth()
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [entraTenantId, setEntraTenantId] = useState('')

  const create = useMutation({
    mutationFn: () =>
      api.post<Tenant>('/admin/tenants', {
        name,
        slug,
        entra_tenant_id: entraTenantId.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t('tenants.created'))
      void qc.invalidateQueries({ queryKey: ['admin-tenants'] })
      // Der neue Kunde muss sofort im Switcher auftauchen (liest user.switchable_tenants) —
      // ohne Refresh bliebe die Sidebar bis zum nächsten Login/Reload veraltet.
      void refresh()
      setName('')
      setSlug('')
      setEntraTenantId('')
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const slugValid = SLUG_PATTERN.test(slug)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.createTitle')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>{t('tenants.name')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.slug')}</Label>
            <Input
              value={slug}
              onChange={(e) => setSlug(e.target.value.toLowerCase())}
              placeholder="kunde-ag"
            />
            <p className="text-muted-foreground text-xs">{t('tenants.slugHint')}</p>
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.entraTenantId')}</Label>
            <Input value={entraTenantId} onChange={(e) => setEntraTenantId(e.target.value)} />
            <p className="text-muted-foreground text-xs">{t('tenants.entraTenantIdHint')}</p>
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={name.trim().length < 1 || !slugValid}
          >
            {t('tenants.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function EditDialog({
  tenant,
  onOpenChange,
}: {
  tenant: Tenant
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { refresh } = useAuth()
  const isDefault = tenant.slug === 'default'
  const [name, setName] = useState(tenant.name)
  const [entraTenantId, setEntraTenantId] = useState(tenant.entra_tenant_id ?? '')
  const [isActive, setIsActive] = useState(tenant.is_active)

  const update = useMutation({
    mutationFn: () =>
      api.patch<Tenant>(`/admin/tenants/${tenant.id}`, {
        name,
        entra_tenant_id: entraTenantId.trim() || null,
        // Der Standard-Kunde bleibt immer aktiv -- der Switch ist für ihn gesperrt,
        // hier zur Sicherheit trotzdem erzwungen (falls der Zustand doch geändert würde).
        is_active: isDefault ? true : isActive,
      }),
    onSuccess: () => {
      toast.success(t('tenants.updated'))
      void qc.invalidateQueries({ queryKey: ['admin-tenants'] })
      // Falls der aktive Kunde umbenannt/deaktiviert wurde, muss der Switcher das sofort
      // zeigen (liest user.active_tenant/switchable_tenants).
      void refresh()
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.editTitle', { name: tenant.name })}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>{t('tenants.name')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.slug')}</Label>
            <Input value={tenant.slug} disabled />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.entraTenantId')}</Label>
            <Input value={entraTenantId} onChange={(e) => setEntraTenantId(e.target.value)} />
            <p className="text-muted-foreground text-xs">{t('tenants.entraTenantIdHint')}</p>
          </div>
          <label className="flex items-center justify-between gap-3 pt-1">
            <span className="text-sm font-medium">{t('tenants.isActive')}</span>
            <Switch
              checked={isActive}
              onCheckedChange={setIsActive}
              disabled={isDefault}
              title={isDefault ? t('tenants.defaultProtected') : undefined}
            />
          </label>
          {isDefault && (
            <p className="text-muted-foreground text-xs">{t('tenants.defaultProtected')}</p>
          )}
        </div>
        <DialogFooter>
          <Button
            onClick={() => update.mutate()}
            loading={update.isPending}
            disabled={name.trim().length < 1}
          >
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DeleteDialog({
  tenant,
  onOpenChange,
}: {
  tenant: Tenant
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const { refresh } = useAuth()

  const del = useMutation({
    mutationFn: () => api.del<{ message: string }>(`/admin/tenants/${tenant.id}`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-tenants'] })
      // Ein gelöschter Kunde darf nicht mehr im Switcher auftauchen — sofort aktualisieren,
      // ohne Reload (liest user.switchable_tenants).
      void refresh()
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.deleteConfirmTitle')}</DialogTitle>
        </DialogHeader>
        <p className="text-muted-foreground text-sm">
          {t('tenants.deleteConfirmDescription', {
            name: tenant.name,
            count: tenant.sso_user_count,
          })}
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button variant="destructive" onClick={() => del.mutate()} loading={del.isPending}>
            {t('tenants.deleteConfirmAction')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Zuweisungen (Access-Modell/Superadmin-Phase, Task 6): welche aktiven Kunden ein
 *  Admin-/Auditor-Konto zusätzlich verwalten/einsehen darf. Superadmins tauchen hier
 *  bewusst nicht auf -- sie sehen ohnehin alle aktiven Kunden (`/admin/users` liefert sie
 *  bereits getrennt in `superadmins`, s. `SuperadminsSection`). */
function AssignmentsSection({ tenants }: { tenants: Tenant[] }) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState<AdminUser | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.get<AdminUsers>('/admin/users'),
  })

  const accounts = [...(data?.local ?? []), ...(data?.sso ?? [])].filter(
    (u) => u.role === 'admin' || u.role === 'auditor',
  )

  return (
    <div className="mt-8">
      <h3 className="text-muted-foreground mb-2 text-xs font-semibold tracking-wide uppercase">
        {t('tenants.assignments.title')}{' '}
        <span className="text-muted-foreground/60">({accounts.length})</span>
      </h3>
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('tenants.assignments.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.assignments.colUsername')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.assignments.colRole')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.assignments.colTenants')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                <tr>
                  <td colSpan={5} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : accounts.length === 0 ? (
                <tr>
                  <td colSpan={5} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.assignments.empty')}
                  </td>
                </tr>
              ) : (
                accounts.map((u) => (
                  <AssignmentRow
                    key={u.id}
                    account={u}
                    tenants={tenants}
                    onEdit={() => setEditing(u)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {editing && (
        <AssignmentDialog
          key={editing.id}
          account={editing}
          tenants={tenants}
          onOpenChange={(o) => !o && setEditing(null)}
        />
      )}
    </div>
  )
}

function AssignmentRow({
  account,
  tenants,
  onEdit,
}: {
  account: AdminUser
  tenants: Tenant[]
  onEdit: () => void
}) {
  const { t } = useTranslation()
  const { data, isLoading } = useQuery({
    queryKey: ['admin-assignments', account.id],
    queryFn: () => api.get<Assignment>(`/admin/assignments/${account.id}`),
  })
  const tenantName = (id: number) => tenants.find((tn) => tn.id === id)?.name ?? `#${id}`

  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2.5 font-medium">{account.display_name || '—'}</td>
      <td className="text-muted-foreground max-w-[220px] truncate px-4 py-2.5 font-mono text-xs">
        {account.username}
      </td>
      <td className="px-4 py-2.5">
        {account.role === 'admin' ? t('access.roleAdmin') : t('access.roleAuditor')}
      </td>
      <td className="px-4 py-2.5">
        {isLoading ? (
          <Skeleton className="h-5 w-32" />
        ) : (data?.tenant_ids.length ?? 0) === 0 ? (
          <span className="text-muted-foreground text-xs">{t('tenants.assignments.none')}</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {data?.tenant_ids.map((id) => (
              <Badge key={id} variant="secondary">
                {tenantName(id)}
              </Badge>
            ))}
          </div>
        )}
      </td>
      <td className="px-4 py-2.5">
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="icon"
            onClick={onEdit}
            aria-label={t('tenants.assignments.edit')}
            title={t('tenants.assignments.edit')}
          >
            <Pencil className="size-4" />
          </Button>
        </div>
      </td>
    </tr>
  )
}

function AssignmentDialog({
  account,
  tenants,
  onOpenChange,
}: {
  account: AdminUser
  tenants: Tenant[]
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [selected, setSelected] = useState<number[]>([])

  const { data, isLoading } = useQuery({
    queryKey: ['admin-assignments', account.id],
    queryFn: () => api.get<Assignment>(`/admin/assignments/${account.id}`),
  })

  // Checkliste einmal mit dem geladenen Bestand vorbelegen -- danach steuert nur noch
  // die lokale Auswahl (kein Re-Sync bei Refetch, sonst würden ungespeicherte Klicks
  // verworfen, sobald React Query im Hintergrund neu lädt).
  useEffect(() => {
    if (data) setSelected(data.tenant_ids)
  }, [data])

  const activeTenants = tenants.filter((tn) => tn.is_active)

  const save = useMutation({
    mutationFn: () =>
      api.put<Assignment>(`/admin/assignments/${account.id}`, { tenant_ids: selected }),
    onSuccess: () => {
      toast.success(t('tenants.assignments.saved'))
      void qc.invalidateQueries({ queryKey: ['admin-assignments', account.id] })
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const toggle = (id: number) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t('tenants.assignments.editTitle', {
              name: account.display_name || account.username,
            })}
          </DialogTitle>
        </DialogHeader>
        <div className="max-h-80 space-y-1 overflow-y-auto">
          {isLoading ? (
            <Skeleton className="h-8 w-full" />
          ) : activeTenants.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              {t('tenants.assignments.noActiveTenants')}
            </p>
          ) : (
            activeTenants.map((tn) => (
              <label
                key={tn.id}
                className="hover:bg-muted/40 flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5"
              >
                <Checkbox
                  checked={selected.includes(tn.id)}
                  onCheckedChange={() => toggle(tn.id)}
                />
                <span className="text-sm">{tn.name}</span>
              </label>
            ))
          )}
        </div>
        <DialogFooter>
          <Button onClick={() => save.mutate()} loading={save.isPending} disabled={isLoading}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Superadmin-Verwaltung (Access-Modell/Superadmin-Phase, Task 6): anlegen (immer lokal),
 *  herabstufen/löschen -- der letzte Superadmin ist serverseitig geschützt (409), die UI
 *  spiegelt das defensiv (Buttons deaktiviert), analog zum Standard-Kunden-Schutz oben. */
function SuperadminsSection() {
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
    <div className="mt-8">
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

function CreateSuperadminDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const create = useMutation({
    mutationFn: () =>
      api.post<AdminUser>('/admin/users/superadmin', {
        username,
        password,
        display_name: `${firstName} ${lastName}`.trim() || null,
      }),
    onSuccess: () => {
      toast.success(t('tenants.superadmins.created'))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      setFirstName('')
      setLastName('')
      setUsername('')
      setPassword('')
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.superadmins.createTitle')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
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
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={username.length < 3 || password.length < 10}
          >
            {t('tenants.superadmins.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
