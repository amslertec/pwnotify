import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { PageHeader } from '@/components/page-header'
import { StatusDot } from '@/components/status-badge'
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
import { Switch } from '@/components/ui/switch'
import { api } from '@/lib/api'
import { hasAdminRights, useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtDate } from '@/lib/format'
import type { Tenant } from '@/lib/types'

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

  const del = useMutation({
    mutationFn: () => api.del<{ message: string }>(`/admin/tenants/${tenant.id}`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-tenants'] })
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
