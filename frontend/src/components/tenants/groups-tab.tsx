import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Trash2, Users } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

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
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import type { AssignmentGroup, Tenant } from '@/lib/types'

/** Gruppen-Tab ("Teams", Console+Groups+Invite-Phase Task 3/7): Entra-Security-Gruppen
 *  des Provider-Tenants, gemappt auf einen oder mehrere Kunden. Mitgliedschaft wird in
 *  Entra gepflegt (`entra_group_id` ist in diesem Inkrement Freitext, kein Graph-Picker,
 *  s. Backend-Moduldoku `admin_groups.py`) -- diese UI verwaltet nur die Kunden-Zuordnung,
 *  der eigentliche Login-Reconcile (welche Grants daraus entstehen) ist Backend Task 4. */
export function GroupsTab({ tenants }: { tenants: Tenant[] }) {
  const { t } = useTranslation()
  const [createOpen, setCreateOpen] = useState(false)
  const [renaming, setRenaming] = useState<AssignmentGroup | null>(null)
  const [assigning, setAssigning] = useState<AssignmentGroup | null>(null)
  const [deleting, setDeleting] = useState<AssignmentGroup | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-groups'],
    queryFn: () => api.get<AssignmentGroup[]>('/admin/groups'),
  })

  const groups = data ?? []
  const tenantName = (id: number) => tenants.find((tn) => tn.id === id)?.name ?? `#${id}`

  return (
    <div>
      <div className="mb-3 flex items-start justify-between gap-4">
        <p className="text-muted-foreground max-w-2xl text-sm">{t('tenants.groups.helper')}</p>
        <Button onClick={() => setCreateOpen(true)} className="shrink-0">
          <Plus /> {t('tenants.groups.create')}
        </Button>
      </div>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colEntraGroupId')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colCustomers')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                <tr>
                  <td colSpan={4} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : groups.length === 0 ? (
                <tr>
                  <td colSpan={4} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.groups.empty')}
                  </td>
                </tr>
              ) : (
                groups.map((g) => (
                  <tr key={g.id} className="hover:bg-muted/30">
                    <td className="px-4 py-2.5 font-medium">{g.name}</td>
                    <td className="text-muted-foreground px-4 py-2.5 font-mono text-xs">
                      {g.entra_group_id}
                    </td>
                    <td className="px-4 py-2.5">
                      {g.tenant_ids.length === 0 ? (
                        <span className="text-muted-foreground text-xs">
                          {t('tenants.groups.none')}
                        </span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {g.tenant_ids.map((id) => (
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
                          onClick={() => setAssigning(g)}
                          aria-label={t('tenants.groups.assignCustomers')}
                          title={t('tenants.groups.assignCustomers')}
                        >
                          <Users className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => setRenaming(g)}
                          aria-label={t('tenants.groups.edit')}
                          title={t('tenants.groups.edit')}
                        >
                          <Pencil className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => setDeleting(g)}
                          aria-label={t('common.delete')}
                          title={t('common.delete')}
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

      <CreateGroupDialog open={createOpen} onOpenChange={setCreateOpen} />
      {renaming && (
        <RenameGroupDialog
          key={renaming.id}
          group={renaming}
          onOpenChange={(o) => !o && setRenaming(null)}
        />
      )}
      {assigning && (
        <AssignTenantsDialog
          key={assigning.id}
          group={assigning}
          tenants={tenants}
          onOpenChange={(o) => !o && setAssigning(null)}
        />
      )}
      {deleting && (
        <DeleteGroupDialog
          key={deleting.id}
          group={deleting}
          onOpenChange={(o) => !o && setDeleting(null)}
        />
      )}
    </div>
  )
}

function CreateGroupDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [entraGroupId, setEntraGroupId] = useState('')

  const create = useMutation({
    mutationFn: () =>
      api.post<AssignmentGroup>('/admin/groups', { name, entra_group_id: entraGroupId }),
    onSuccess: () => {
      toast.success(t('tenants.groups.created'))
      void qc.invalidateQueries({ queryKey: ['admin-groups'] })
      setName('')
      setEntraGroupId('')
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.groups.createTitle')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>{t('tenants.groups.name')}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.groups.entraGroupId')}</Label>
            <Input value={entraGroupId} onChange={(e) => setEntraGroupId(e.target.value)} />
            <p className="text-muted-foreground text-xs">{t('tenants.groups.entraGroupIdHint')}</p>
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={name.trim().length < 1 || entraGroupId.trim().length < 1}
          >
            {t('tenants.groups.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RenameGroupDialog({
  group,
  onOpenChange,
}: {
  group: AssignmentGroup
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [name, setName] = useState(group.name)

  const update = useMutation({
    mutationFn: () => api.put<AssignmentGroup>(`/admin/groups/${group.id}`, { name }),
    onSuccess: () => {
      toast.success(t('tenants.groups.updated'))
      void qc.invalidateQueries({ queryKey: ['admin-groups'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.groups.editTitle', { name: group.name })}</DialogTitle>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label>{t('tenants.groups.name')}</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
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

function AssignTenantsDialog({
  group,
  tenants,
  onOpenChange,
}: {
  group: AssignmentGroup
  tenants: Tenant[]
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // `group` kommt als bereits geladenes Prop rein (kein eigener Query-Fetch in diesem
  // Dialog) -- die Elternkomponente montiert per `key={group.id}` neu, wenn eine andere
  // Gruppe geöffnet wird, daher genügt die einmalige Initialisierung aus dem Prop
  // (kein Re-Sync-Effect nötig, anders als bei `AssignmentDialog`, das selbst pollt).
  const [selected, setSelected] = useState<number[]>(group.tenant_ids)

  const activeTenants = tenants.filter((tn) => tn.is_active)

  const save = useMutation({
    mutationFn: () =>
      api.put<AssignmentGroup>(`/admin/groups/${group.id}/tenants`, { tenant_ids: selected }),
    onSuccess: () => {
      toast.success(t('tenants.groups.saved'))
      void qc.invalidateQueries({ queryKey: ['admin-groups'] })
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
          <DialogTitle>{t('tenants.groups.assignCustomersTitle', { name: group.name })}</DialogTitle>
        </DialogHeader>
        <div className="max-h-80 space-y-1 overflow-y-auto">
          {activeTenants.length === 0 ? (
            <p className="text-muted-foreground text-sm">{t('tenants.groups.noActiveTenants')}</p>
          ) : (
            activeTenants.map((tn) => (
              <label
                key={tn.id}
                className="hover:bg-muted/40 flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5"
              >
                <Checkbox checked={selected.includes(tn.id)} onCheckedChange={() => toggle(tn.id)} />
                <span className="text-sm">{tn.name}</span>
              </label>
            ))
          )}
        </div>
        <DialogFooter>
          <Button onClick={() => save.mutate()} loading={save.isPending}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DeleteGroupDialog({
  group,
  onOpenChange,
}: {
  group: AssignmentGroup
  onOpenChange: (o: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const del = useMutation({
    mutationFn: () => api.del<{ message: string }>(`/admin/groups/${group.id}`),
    onSuccess: () => {
      toast.success(t('tenants.groups.deleted'))
      void qc.invalidateQueries({ queryKey: ['admin-groups'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('tenants.groups.deleteConfirmTitle')}</DialogTitle>
        </DialogHeader>
        <p className="text-muted-foreground text-sm">
          {t('tenants.groups.deleteConfirmDescription', { name: group.name })}
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button variant="destructive" onClick={() => del.mutate()} loading={del.isPending}>
            {t('tenants.groups.deleteConfirmAction')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
