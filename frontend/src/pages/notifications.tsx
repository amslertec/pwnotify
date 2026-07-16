import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, ChevronDown, RotateCw, Search, X } from 'lucide-react'
import { Fragment, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtDateTime } from '@/lib/format'
import type { Notification, Page } from '@/lib/types'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 25

export default function NotificationsPage() {
  const { t } = useTranslation()
  const isAdmin = useAuth().user?.role === 'admin'
  const qc = useQueryClient()
  const [status, setStatus] = useState('all')
  const [search, setSearch] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState<number | null>(null)

  // Suche entprellen — nicht bei jedem Tastendruck eine Anfrage.
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(id)
  }, [search])

  // Jede Filteränderung führt zurück auf Seite 1.
  useEffect(() => setPage(1), [status, debouncedSearch, dateFrom, dateTo])

  const params = useMemo(() => {
    const p = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (status !== 'all') p.set('status', status)
    if (debouncedSearch) p.set('search', debouncedSearch)
    if (dateFrom) p.set('date_from', dateFrom)
    if (dateTo) p.set('date_to', dateTo)
    return p.toString()
  }, [page, status, debouncedSearch, dateFrom, dateTo])

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['notifications', params],
    queryFn: () => api.get<Page<Notification>>(`/notifications?${params}`),
    placeholderData: (p) => p,
  })

  const retry = useMutation({
    mutationFn: (id: number) => api.post<{ message: string }>(`/notifications/${id}/retry`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['notifications'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))
  const hasFilters = !!debouncedSearch || !!dateFrom || !!dateTo || status !== 'all'

  const clearFilters = () => {
    setSearch('')
    setDateFrom('')
    setDateTo('')
    setStatus('all')
  }

  return (
    <div>
      <PageHeader title={t('notifications.title')} description={t('notifications.description')} />

      {/* Filterleiste: Suche, Datumsbereich, Status */}
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="min-w-[220px] flex-1">
          <label className="text-muted-foreground mb-1 block text-xs font-medium">
            {t('notifications.filter.searchLabel')}
          </label>
          <div className="relative">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('notifications.filter.searchPlaceholder')}
              className="pl-9"
            />
          </div>
        </div>
        <div>
          <label className="text-muted-foreground mb-1 block text-xs font-medium">
            {t('notifications.filter.from')}
          </label>
          <Input
            type="date"
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(e) => setDateFrom(e.target.value)}
            className="w-40"
          />
        </div>
        <div>
          <label className="text-muted-foreground mb-1 block text-xs font-medium">
            {t('notifications.filter.to')}
          </label>
          <Input
            type="date"
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(e) => setDateTo(e.target.value)}
            className="w-40"
          />
        </div>
        <div>
          <label className="text-muted-foreground mb-1 block text-xs font-medium">
            {t('notifications.filter.statusLabel')}
          </label>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('notifications.filter.all')}</SelectItem>
              <SelectItem value="sent">{t('notifications.filter.sent')}</SelectItem>
              <SelectItem value="failed">{t('notifications.filter.failed')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {hasFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            <X className="size-4" /> {t('notifications.filter.clear')}
          </Button>
        )}
      </div>

      <Card className={cn('overflow-hidden', isFetching && 'opacity-70 transition-opacity')}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs tracking-wide uppercase">
                <th className="px-4 py-3 font-medium">{t('notifications.columns.time')}</th>
                <th className="px-4 py-3 font-medium">{t('notifications.columns.recipient')}</th>
                <th className="px-4 py-3 font-medium">{t('notifications.columns.stage')}</th>
                <th className="px-4 py-3 font-medium">{t('notifications.columns.language')}</th>
                <th className="px-4 py-3 font-medium">{t('notifications.columns.status')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i}>
                    <td colSpan={6} className="px-4 py-3">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState
                      icon={Bell}
                      title={
                        hasFilters
                          ? t('notifications.empty.filteredTitle')
                          : t('notifications.empty.title')
                      }
                      description={
                        hasFilters
                          ? t('notifications.empty.filteredDescription')
                          : t('notifications.empty.description')
                      }
                    />
                  </td>
                </tr>
              ) : (
                rows.map((n) => {
                  const ok = n.status === 'sent'
                  const open = expanded === n.id
                  return (
                    <Fragment key={n.id}>
                      <tr className="hover:bg-muted/30">
                        <td className="px-4 py-2.5 whitespace-nowrap">
                          {fmtDateTime(n.created_at)}
                        </td>
                        <td className="max-w-[320px] truncate px-4 py-2.5">{n.recipient}</td>
                        <td className="px-4 py-2.5 whitespace-nowrap">
                          {t('notifications.days', { n: n.reminder_day })}
                        </td>
                        <td className="text-muted-foreground px-4 py-2.5 uppercase">
                          {n.language}
                        </td>
                        <td className="px-4 py-2.5">
                          <Badge variant={ok ? 'success' : 'danger'}>
                            {ok
                              ? t('notifications.status.sent')
                              : t('notifications.status.failed')}
                          </Badge>
                        </td>
                        <td className="px-4 py-2.5">
                          {n.status === 'failed' && (
                            <div className="flex justify-end gap-1">
                              {isAdmin && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => retry.mutate(n.id)}
                                  aria-label={t('notifications.actions.retry')}
                                >
                                  <RotateCw className="size-3.5" />
                                </Button>
                              )}
                              {n.error && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => setExpanded(open ? null : n.id)}
                                  aria-label={t('notifications.actions.showError')}
                                >
                                  <ChevronDown
                                    className={cn(
                                      'size-3.5 transition-transform',
                                      open && 'rotate-180',
                                    )}
                                  />
                                </Button>
                              )}
                            </div>
                          )}
                        </td>
                      </tr>
                      {open && n.error && (
                        <tr className="bg-danger/5">
                          <td
                            colSpan={6}
                            className="text-danger px-4 py-2 font-mono text-xs break-words"
                          >
                            {n.error}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="border-border flex items-center justify-between border-t px-4 py-3 text-sm">
          <span className="text-muted-foreground">
            {t('notifications.pagination.summary', {
              count: data?.total ?? 0,
              page,
              total: totalPages,
            })}
          </span>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              {t('notifications.pagination.prev')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              {t('notifications.pagination.next')}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
