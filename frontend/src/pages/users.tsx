import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, RefreshCw, Search, SlidersHorizontal, Users as UsersIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'

import { UserDrawer } from '@/components/user-drawer'
import { DaysBadge, StatusDot } from '@/components/status-badge'
import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import { fmtDate } from '@/lib/format'
import type { EntraUser, Page } from '@/lib/types'
import { cn, initials } from '@/lib/utils'

const COLUMNS = [
  { key: 'mail', label: 'Primär-Mail' },
  { key: 'other_mails', label: 'Alternativ-Mail' },
  { key: 'last_password_change', label: 'Zuletzt geändert' },
  { key: 'expiry_date', label: 'Ablaufdatum' },
  { key: 'account_enabled', label: 'Konto' },
] as const

const STATUS_OPTIONS = [
  { value: 'all', label: 'Alle Status' },
  { value: 'ok', label: 'OK (> 14 T)' },
  { value: 'soon', label: 'Bald (≤ 7 T)' },
  { value: 'expired', label: 'Abgelaufen' },
  { value: 'never', label: 'Kein Ablauf' },
  { value: 'excluded', label: 'Ausgeschlossen' },
]

export default function UsersPage() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [status, setStatus] = useState('all')
  const [page, setPage] = useState(1)
  const [pageSize] = useState(25)
  const [sortBy, setSortBy] = useState('days_left')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [drawerId, setDrawerId] = useState<number | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 300)
    return () => clearTimeout(t)
  }, [search])
  useEffect(() => setPage(1), [debounced, status])

  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
    sort_by: sortBy,
    sort_dir: sortDir,
  })
  if (debounced) params.set('search', debounced)
  if (status !== 'all') params.set('status', status)

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['users', page, pageSize, sortBy, sortDir, debounced, status],
    queryFn: () => api.get<Page<EntraUser>>(`/users?${params.toString()}`),
    placeholderData: (prev) => prev,
  })

  const sync = useMutation({
    mutationFn: () => api.post('/runs/trigger', { dry_run: false }),
    onSuccess: () => {
      toast.success('Lauf gestartet')
      void qc.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  const bulk = useMutation({
    mutationFn: (action: string) =>
      api.post<{ message: string }>('/users/bulk', { ids: [...selected], action }),
    onSuccess: (res) => {
      toast.success(res.message)
      setSelected(new Set())
      void qc.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / pageSize))
  const allSelected = rows.length > 0 && rows.every((r) => selected.has(r.id))

  const toggleSort = (col: string) => {
    if (sortBy === col) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(col)
      setSortDir('asc')
    }
  }

  const exportUrl = (fmt: string) => {
    const p = new URLSearchParams({ fmt })
    if (debounced) p.set('search', debounced)
    if (status !== 'all') p.set('status', status)
    return `/api/users/export?${p.toString()}`
  }

  return (
    <div>
      <PageHeader
        title="Benutzer"
        description="Alle Entra-ID-Benutzer und ihr Passwort-Ablaufstatus."
        actions={
          <>
            <Button variant="outline" onClick={() => sync.mutate()} loading={sync.isPending}>
              <RefreshCw /> Sync
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline">
                  <Download /> Export
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItemLink href={exportUrl('csv')}>Als CSV</DropdownMenuItemLink>
                <DropdownMenuItemLink href={exportUrl('xlsx')}>Als XLSX</DropdownMenuItemLink>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        }
      />

      {/* Filterleiste */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Name, UPN oder Mail suchen…"
            className="pl-9"
          />
        </div>
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon" aria-label="Spalten">
              <SlidersHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Spalten</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {COLUMNS.map((c) => (
              <DropdownMenuCheckboxItem
                key={c.key}
                checked={!hidden.has(c.key)}
                onCheckedChange={(v) =>
                  setHidden((prev) => {
                    const next = new Set(prev)
                    if (v) next.delete(c.key)
                    else next.add(c.key)
                    return next
                  })
                }
              >
                {c.label}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Bulk-Leiste */}
      {selected.size > 0 && (
        <div className="border-primary/30 bg-primary/5 mb-3 flex items-center gap-3 rounded-lg border px-4 py-2 text-sm">
          <span className="font-medium">{selected.size} ausgewählt</span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => bulk.mutate('notify')}
            loading={bulk.isPending}
          >
            Erinnerung senden
          </Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate('exclude')}>
            Ausschliessen
          </Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate('include')}>
            Einschliessen
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setSelected(new Set())}
            className="ml-auto"
          >
            Aufheben
          </Button>
        </div>
      )}

      <Card className={cn('overflow-hidden', isFetching && 'opacity-70 transition-opacity')}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs tracking-wide uppercase">
                <th className="w-10 px-4 py-3">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={(v) =>
                      setSelected((prev) => {
                        const next = new Set(prev)
                        rows.forEach((r) => (v ? next.add(r.id) : next.delete(r.id)))
                        return next
                      })
                    }
                    aria-label="Alle auswählen"
                  />
                </th>
                <SortableTh label="Name" col="display_name" {...{ sortBy, sortDir, toggleSort }} />
                <th className="px-4 py-3 font-medium">UPN</th>
                {!hidden.has('mail') && <th className="px-4 py-3 font-medium">Primär-Mail</th>}
                {!hidden.has('other_mails') && <th className="px-4 py-3 font-medium">Alt.-Mail</th>}
                {!hidden.has('last_password_change') && (
                  <SortableTh
                    label="Geändert"
                    col="last_password_change"
                    {...{ sortBy, sortDir, toggleSort }}
                  />
                )}
                {!hidden.has('expiry_date') && (
                  <SortableTh
                    label="Ablauf"
                    col="expiry_date"
                    {...{ sortBy, sortDir, toggleSort }}
                  />
                )}
                <SortableTh label="Rest" col="days_left" {...{ sortBy, sortDir, toggleSort }} />
                {!hidden.has('account_enabled') && <th className="px-4 py-3 font-medium">Konto</th>}
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i}>
                    <td colSpan={9} className="px-4 py-3">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={9}>
                    <EmptyState
                      icon={UsersIcon}
                      title="Keine Benutzer"
                      description="Starten Sie einen Sync, um Benutzer aus Entra ID zu laden."
                    />
                  </td>
                </tr>
              ) : (
                rows.map((u) => (
                  <tr
                    key={u.id}
                    className="hover:bg-muted/40 cursor-pointer transition-colors"
                    onClick={() => setDrawerId(u.id)}
                  >
                    <td className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                      <Checkbox
                        checked={selected.has(u.id)}
                        onCheckedChange={(v) =>
                          setSelected((prev) => {
                            const next = new Set(prev)
                            if (v) next.add(u.id)
                            else next.delete(u.id)
                            return next
                          })
                        }
                        aria-label="Auswählen"
                      />
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2.5">
                        <span className="bg-primary/10 text-primary grid size-8 shrink-0 place-items-center rounded-full text-xs font-semibold">
                          {initials(u.display_name)}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate font-medium">{u.display_name}</p>
                          {(u.is_shared || u.excluded) && (
                            <div className="mt-0.5 flex gap-1">
                              {u.is_shared && <Badge variant="secondary">Shared</Badge>}
                              {u.excluded && <Badge variant="secondary">ausgeschlossen</Badge>}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="text-muted-foreground max-w-[220px] truncate px-4 py-2.5 font-mono text-xs">
                      {u.upn}
                    </td>
                    {!hidden.has('mail') && (
                      <td className="text-muted-foreground max-w-[200px] truncate px-4 py-2.5">
                        {u.mail ?? '—'}
                      </td>
                    )}
                    {!hidden.has('other_mails') && (
                      <td className="text-muted-foreground max-w-[200px] truncate px-4 py-2.5">
                        {u.other_mails[0] ?? '—'}
                      </td>
                    )}
                    {!hidden.has('last_password_change') && (
                      <td className="text-muted-foreground px-4 py-2.5">
                        {fmtDate(u.last_password_change)}
                      </td>
                    )}
                    {!hidden.has('expiry_date') && (
                      <td className="text-muted-foreground px-4 py-2.5">
                        {fmtDate(u.expiry_date)}
                      </td>
                    )}
                    <td className="px-4 py-2.5">
                      <DaysBadge user={u} />
                    </td>
                    {!hidden.has('account_enabled') && (
                      <td className="px-4 py-2.5">
                        <span className="inline-flex items-center gap-1.5 text-xs">
                          <StatusDot status={u.account_enabled ? 'ok' : 'disabled'} />
                          {u.account_enabled ? 'Aktiv' : 'Deaktiviert'}
                        </span>
                      </td>
                    )}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        <div className="border-border flex items-center justify-between border-t px-4 py-3 text-sm">
          <span className="text-muted-foreground">
            {data?.total ?? 0} Benutzer · Seite {page}/{totalPages}
          </span>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              Zurück
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Weiter
            </Button>
          </div>
        </div>
      </Card>

      <UserDrawer
        userId={drawerId}
        open={drawerId !== null}
        onOpenChange={(o) => !o && setDrawerId(null)}
      />
    </div>
  )
}

function SortableTh({
  label,
  col,
  sortBy,
  sortDir,
  toggleSort,
}: {
  label: string
  col: string
  sortBy: string
  sortDir: 'asc' | 'desc'
  toggleSort: (c: string) => void
}) {
  return (
    <th className="px-4 py-3 font-medium">
      <button
        className="hover:text-foreground inline-flex items-center gap-1"
        onClick={() => toggleSort(col)}
      >
        {label}
        {sortBy === col && <span className="text-primary">{sortDir === 'asc' ? '↑' : '↓'}</span>}
      </button>
    </th>
  )
}

function DropdownMenuItemLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      className="hover:bg-muted flex cursor-pointer items-center rounded-md px-2 py-1.5 text-sm outline-none select-none"
    >
      {children}
    </a>
  )
}
