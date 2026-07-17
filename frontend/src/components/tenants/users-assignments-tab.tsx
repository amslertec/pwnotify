import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import { useEffect, useState } from 'react'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import type {
  AdminUser,
  AdminUsers,
  Assignment,
  BulkAssignment,
  BulkAssignmentResult,
  Tenant,
} from '@/lib/types'

/** Zuweisungen (Access-Modell/Superadmin-Phase, Task 6; Bulk-Auswahl Console+Groups+
 *  Invite Task 2/7): welche aktiven Kunden ein Admin-/Auditor-Konto zusätzlich verwalten/
 *  einsehen darf. Superadmins tauchen hier bewusst nicht auf -- sie sehen ohnehin alle
 *  aktiven Kunden (`/admin/users` liefert sie bereits getrennt in `superadmins`, s.
 *  `SuperadminsTab`). Die Checkbox-Spalte + Bulk-Leiste ergänzen die bestehende
 *  Einzel-Konto-Bearbeitung (per-Zeile-Stift-Button) -- Gruppen/Bulk ERGÄNZEN die
 *  Pro-Konto-Zuweisung, sie ersetzen sie nicht (Design §2). */
export function UsersAssignmentsTab({ tenants }: { tenants: Tenant[] }) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState<AdminUser | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.get<AdminUsers>('/admin/users'),
  })

  const accounts = [...(data?.local ?? []), ...(data?.sso ?? [])].filter(
    (u) => u.role === 'admin' || u.role === 'auditor',
  )
  const allSelected = accounts.length > 0 && accounts.every((a) => selected.has(a.id))

  const toggleAll = (checked: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      accounts.forEach((a) => (checked ? next.add(a.id) : next.delete(a.id)))
      return next
    })

  const toggleOne = (id: number, checked: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          {t('tenants.assignments.title')}{' '}
          <span className="text-muted-foreground/60">({accounts.length})</span>
        </h3>
      </div>

      {selected.size > 0 && (
        <div className="border-primary/30 bg-primary/5 mb-3 flex items-center gap-3 rounded-lg border px-4 py-2 text-sm">
          <span className="font-medium">
            {t('tenants.assignments.bulk.selectedCount', { n: selected.size })}
          </span>
          <Button size="sm" variant="outline" onClick={() => setBulkOpen(true)}>
            {t('tenants.assignments.bulk.open')}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setSelected(new Set())}
            className="ml-auto"
          >
            {t('tenants.assignments.bulk.clearSelection')}
          </Button>
        </div>
      )}

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="w-10 px-4 py-3">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={(v) => toggleAll(!!v)}
                    aria-label={t('tenants.assignments.bulk.selectAll')}
                  />
                </th>
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
                  <td colSpan={6} className="p-4">
                    <Skeleton className="h-8 w-full" />
                  </td>
                </tr>
              ) : accounts.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-muted-foreground px-4 py-6 text-center">
                    {t('tenants.assignments.empty')}
                  </td>
                </tr>
              ) : (
                accounts.map((u) => (
                  <AssignmentRow
                    key={u.id}
                    account={u}
                    tenants={tenants}
                    checked={selected.has(u.id)}
                    onToggle={(v) => toggleOne(u.id, v)}
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

      {bulkOpen && (
        <BulkAssignDialog
          userIds={[...selected]}
          accounts={accounts}
          tenants={tenants}
          onOpenChange={setBulkOpen}
          onDone={() => {
            setSelected(new Set())
            setBulkOpen(false)
          }}
        />
      )}
    </div>
  )
}

function AssignmentRow({
  account,
  tenants,
  checked,
  onToggle,
  onEdit,
}: {
  account: AdminUser
  tenants: Tenant[]
  checked: boolean
  onToggle: (v: boolean) => void
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
      <td className="px-4 py-2.5">
        <Checkbox
          checked={checked}
          onCheckedChange={(v) => onToggle(!!v)}
          aria-label={t('tenants.assignments.bulk.select')}
        />
      </td>
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

/** Bulk-Zuweisungsdialog (Task 2/7): EIN `PUT /admin/assignments/bulk` für alle
 *  ausgewählten Konten. Zwei Phasen im selben Dialog -- erst das Formular (Aktion +
 *  Kunden-Checkliste), nach dem Absenden das Ergebnis (aktualisierte Anzahl + ggf.
 *  übersprungene Konten mit übersetztem Grund). Die `reason`-Werte
 *  (`customer_account_not_grantable`/`cannot_assign_superadmin`/`user_not_found`) sind
 *  bereits als `errors.<code>`-Keys vorhanden (Einzel-Zuweisungs-Fehler) -- hier wird
 *  derselbe Namensraum wiederverwendet statt eigener Übersetzungen. */
function BulkAssignDialog({
  userIds,
  accounts,
  tenants,
  onOpenChange,
  onDone,
}: {
  userIds: number[]
  accounts: AdminUser[]
  tenants: Tenant[]
  onOpenChange: (o: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [action, setAction] = useState<BulkAssignment['action']>('add')
  const [tenantIds, setTenantIds] = useState<number[]>([])
  const [result, setResult] = useState<BulkAssignmentResult | null>(null)

  const activeTenants = tenants.filter((tn) => tn.is_active)
  const accountName = (id: number) => {
    const acc = accounts.find((a) => a.id === id)
    return acc ? acc.display_name || acc.username : `#${id}`
  }

  const toggleTenant = (id: number) =>
    setTenantIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const submit = useMutation({
    mutationFn: () =>
      api.put<BulkAssignmentResult>('/admin/assignments/bulk', {
        user_ids: userIds,
        tenant_ids: tenantIds,
        action,
      } satisfies BulkAssignment),
    onSuccess: (r) => {
      setResult(r)
      if (r.updated.length > 0) {
        toast.success(t('tenants.assignments.bulk.updatedToast', { count: r.updated.length }))
        void qc.invalidateQueries({ queryKey: ['admin-assignments'] })
        void qc.invalidateQueries({ queryKey: ['admin-users'] })
      }
      // Keine übersprungenen Konten -- Dialog kann direkt schliessen, sonst bleibt er
      // offen, damit die Liste der übersprungenen Konten sichtbar bleibt.
      if (r.skipped.length === 0) onDone()
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t('tenants.assignments.bulk.dialogTitle', { count: userIds.length })}
          </DialogTitle>
        </DialogHeader>

        {result ? (
          <div className="space-y-3">
            <p className="text-sm">
              {t('tenants.assignments.bulk.updatedToast', { count: result.updated.length })}
            </p>
            {result.skipped.length > 0 && (
              <div className="border-warning/40 bg-warning/5 space-y-2 rounded-md border p-3">
                <p className="text-sm font-medium">
                  {t('tenants.assignments.bulk.skippedTitle', { count: result.skipped.length })}
                </p>
                <p className="text-muted-foreground text-xs">
                  {t('tenants.assignments.bulk.skippedHint')}
                </p>
                <ul className="space-y-1 text-sm">
                  {result.skipped.map((s) => (
                    <li key={s.user_id} className="flex items-center justify-between gap-3">
                      <span className="font-medium">{accountName(s.user_id)}</span>
                      <span className="text-muted-foreground text-xs">
                        {t(`errors.${s.reason}`, { defaultValue: s.reason })}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">
                {t('tenants.assignments.bulk.actionLabel')}
              </label>
              <Select
                value={action}
                onValueChange={(v) => setAction(v as BulkAssignment['action'])}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="add">{t('tenants.assignments.bulk.actionAdd')}</SelectItem>
                  <SelectItem value="remove">
                    {t('tenants.assignments.bulk.actionRemove')}
                  </SelectItem>
                  <SelectItem value="set">{t('tenants.assignments.bulk.actionSet')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">
                {t('tenants.assignments.bulk.customersLabel')}
              </label>
              <div className="max-h-72 space-y-1 overflow-y-auto">
                {activeTenants.length === 0 ? (
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
                        checked={tenantIds.includes(tn.id)}
                        onCheckedChange={() => toggleTenant(tn.id)}
                      />
                      <span className="text-sm">{tn.name}</span>
                    </label>
                  ))
                )}
              </div>
            </div>
          </div>
        )}

        <DialogFooter>
          {result ? (
            <Button onClick={onDone}>{t('tenants.assignments.bulk.done')}</Button>
          ) : (
            <Button onClick={() => submit.mutate()} loading={submit.isPending}>
              {t('tenants.assignments.bulk.submit')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
