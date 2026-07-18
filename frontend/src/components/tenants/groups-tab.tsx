import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, ChevronDown, Pencil, Plus, RefreshCw, Trash2, Users } from 'lucide-react'
import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { AvatarImage } from '@/components/avatar-image'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { fmtDateTime } from '@/lib/format'
import type { AssignmentGroup, GroupMember, GroupMemberPage, GroupSyncResult, Tenant } from '@/lib/types'
import { cn } from '@/lib/utils'

/** Feste Seitengrösse für die Mitgliederliste -- der Server 422-t ab `size > 200`, wir
 *  bleiben bewusst klein für UI-Paging (Backend-Contract Task 4, s. Moduldoku `admin_groups.py`). */
export const MEMBERS_PAGE_SIZE = 25

/** Pfad für `GET /admin/groups/{id}/members` mit fixer `size`. */
export function groupMembersPath(groupId: number, page: number): string {
  return `/admin/groups/${groupId}/members?page=${page}&size=${MEMBERS_PAGE_SIZE}`
}

/** Query-Key für die Mitgliederliste -- gescoped auf Gruppe UND Seite, damit ein
 *  Seitenwechsel (oder parallel geöffnete Gruppen) sich nicht gegenseitig überschreiben. */
export function groupMembersQueryKey(groupId: number, page: number) {
  return ['group-members', groupId, page] as const
}

/** Pfad für `POST /admin/groups/{id}/sync`. */
export function groupSyncPath(groupId: number): string {
  return `/admin/groups/${groupId}/sync`
}

/** Pfad für `GET /api/entra-avatar/{entra_id}` -- liefert 404, falls kein Foto vorhanden
 *  (AvatarImage fällt dann automatisch auf Initialen zurück). */
export function entraAvatarPath(entraId: string): string {
  return `/api/entra-avatar/${entraId}`
}

/** true, sobald die aktuelle Seite die letzte ist (`page * size >= total`) -- steuert das
 *  Deaktivieren des "Weiter"-Buttons in der Mitglieder-Pagination. */
export function isLastMembersPage(page: number, total: number, size = MEMBERS_PAGE_SIZE): boolean {
  return page * size >= total
}

/** Anzeigename eines Mitglieds: `display_name`, sonst Fallback auf die UPN. */
export function memberDisplayName(member: Pick<GroupMember, 'display_name' | 'upn'>): string {
  return member.display_name ?? member.upn
}

/** true, wenn die Gruppe noch nie synchronisiert wurde (`last_synced_at === null`) --
 *  steuert den "noch nie synchronisiert"-Platzhalter in der Tabelle. */
export function hasNeverSynced(lastSyncedAt: string | null): boolean {
  return lastSyncedAt === null
}

/** Mappt ein Sync-Ergebnis auf die Interpolationswerte des Erfolgs-Toasts. */
export function syncToastParams(result: GroupSyncResult): { count: number; materialized: number } {
  return { count: result.member_count, materialized: result.materialized }
}

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
                <th className="px-4 py-3" />
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colName')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colEntraGroupId')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colCustomers')}</th>
                <th className="px-4 py-3 font-medium">{t('tenants.groups.colMembers')}</th>
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
              ) : groups.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.groups.empty')}
                  </td>
                </tr>
              ) : (
                groups.map((g) => (
                  <GroupRow
                    key={g.id}
                    group={g}
                    tenantName={tenantName}
                    onAssign={() => setAssigning(g)}
                    onRename={() => setRenaming(g)}
                    onDelete={() => setDeleting(g)}
                  />
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

/** Eine Gruppenzeile inkl. Chevron-Expander (Mitgliederliste) und Sync-Button
 *  (Group-Member-Sync, Task 4/5). Eigene Komponente, damit `expanded`/`page` lokaler
 *  State je Zeile bleiben (Muster wie `AuditRow`/die Notifications-Zeile). */
function GroupRow({
  group,
  tenantName,
  onAssign,
  onRename,
  onDelete,
}: {
  group: AssignmentGroup
  tenantName: (id: number) => string
  onAssign: () => void
  onRename: () => void
  onDelete: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [page, setPage] = useState(1)

  const sync = useMutation({
    mutationFn: () => api.post<GroupSyncResult>(groupSyncPath(group.id)),
    onSuccess: (result) => {
      toast.success(t('tenants.groups.syncResult', syncToastParams(result)))
      void qc.invalidateQueries({ queryKey: ['admin-groups'] })
      void qc.invalidateQueries({ queryKey: ['group-members', group.id] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Fragment>
      <tr className="hover:bg-muted/30">
        <td className="px-4 py-2.5">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => {
              setExpanded((v) => !v)
              setPage(1)
            }}
            aria-label={
              expanded ? t('tenants.groups.collapseMembers') : t('tenants.groups.expandMembers')
            }
            title={expanded ? t('tenants.groups.collapseMembers') : t('tenants.groups.expandMembers')}
          >
            <ChevronDown className={cn('size-4 transition-transform', expanded && 'rotate-180')} />
          </Button>
        </td>
        <td className="px-4 py-2.5 font-medium">{group.name}</td>
        <td className="text-muted-foreground px-4 py-2.5 font-mono text-xs">
          {group.entra_group_id}
        </td>
        <td className="px-4 py-2.5">
          {group.tenant_ids.length === 0 ? (
            <span className="text-muted-foreground text-xs">{t('tenants.groups.none')}</span>
          ) : (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 gap-1.5 px-2"
                  aria-label={t('tenants.groups.showCustomers', {
                    count: group.tenant_ids.length,
                  })}
                  title={t('tenants.groups.showCustomers', { count: group.tenant_ids.length })}
                >
                  <Building2 className="size-4" />
                  <span className="text-xs font-medium">{group.tenant_ids.length}</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="max-h-64 overflow-y-auto">
                <DropdownMenuLabel>{t('tenants.groups.assignedCustomers')}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {group.tenant_ids.map((id) => (
                  <DropdownMenuItem key={id} className="text-sm">
                    {tenantName(id)}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </td>
        <td className="text-muted-foreground px-4 py-2.5 text-xs">
          <div>{t('tenants.groups.memberCount', { count: group.member_count })}</div>
          <div>
            {hasNeverSynced(group.last_synced_at)
              ? t('tenants.groups.neverSynced')
              : fmtDateTime(group.last_synced_at)}
          </div>
        </td>
        <td className="px-4 py-2.5">
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => sync.mutate()}
              loading={sync.isPending}
              aria-label={t('tenants.groups.sync')}
              title={t('tenants.groups.sync')}
            >
              {!sync.isPending && <RefreshCw className="size-4" />}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={onAssign}
              aria-label={t('tenants.groups.assignCustomers')}
              title={t('tenants.groups.assignCustomers')}
            >
              <Users className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={onRename}
              aria-label={t('tenants.groups.edit')}
              title={t('tenants.groups.edit')}
            >
              <Pencil className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={onDelete}
              aria-label={t('common.delete')}
              title={t('common.delete')}
            >
              <Trash2 className="text-danger size-4" />
            </Button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-muted/20">
          <td colSpan={6} className="px-4 py-3">
            <GroupMembers groupId={group.id} page={page} onPageChange={setPage} />
          </td>
        </tr>
      )}
    </Fragment>
  )
}

/** Paginierte Mitgliederliste einer Gruppe (`GET /admin/groups/{id}/members`). Eigener
 *  Query pro Gruppe+Seite -- `size` fix 25 (s. `MEMBERS_PAGE_SIZE`). */
function GroupMembers({
  groupId,
  page,
  onPageChange,
}: {
  groupId: number
  page: number
  onPageChange: (page: number) => void
}) {
  const { t } = useTranslation()

  const { data, isLoading } = useQuery({
    queryKey: groupMembersQueryKey(groupId, page),
    queryFn: () => api.get<GroupMemberPage>(groupMembersPath(groupId, page)),
    placeholderData: (prev) => prev,
  })

  const members = data?.items ?? []
  const total = data?.total ?? 0

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="border-border text-muted-foreground border-b text-left uppercase">
            <th className="px-2 py-1.5 font-medium">{t('tenants.groups.members.colName')}</th>
            <th className="px-2 py-1.5 font-medium">{t('tenants.groups.members.colUpn')}</th>
            <th className="px-2 py-1.5 font-medium">{t('tenants.groups.members.colMail')}</th>
          </tr>
        </thead>
        <tbody className="divide-border divide-y">
          {isLoading ? (
            <tr>
              <td colSpan={3} className="px-2 py-2">
                <Skeleton className="h-6 w-full" />
              </td>
            </tr>
          ) : members.length === 0 ? (
            <tr>
              <td colSpan={3} className="text-muted-foreground px-2 py-3 text-center">
                {t('tenants.groups.members.empty')}
              </td>
            </tr>
          ) : (
            members.map((m) => (
              <tr key={m.entra_id}>
                <td className="px-2 py-1.5">
                  <div className="flex items-center gap-2">
                    <AvatarImage
                      name={memberDisplayName(m)}
                      src={entraAvatarPath(m.entra_id)}
                      className="size-6 text-[10px]"
                    />
                    {memberDisplayName(m)}
                  </div>
                </td>
                <td className="text-muted-foreground px-2 py-1.5 font-mono">{m.upn}</td>
                <td className="text-muted-foreground px-2 py-1.5">{m.mail ?? '—'}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
      <div className="mt-2 flex items-center justify-between text-xs">
        <span className="text-muted-foreground">
          {t('tenants.groups.members.pagination.summary', { count: total })}
        </span>
        <div className="flex gap-1">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
          >
            {t('tenants.groups.members.pagination.prev')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={isLastMembersPage(page, total)}
            onClick={() => onPageChange(page + 1)}
          >
            {t('tenants.groups.members.pagination.next')}
          </Button>
        </div>
      </div>
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
