import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, ChevronDown, RotateCw } from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { toast } from 'sonner'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import { fmtDateTime } from '@/lib/format'
import type { Notification, Page } from '@/lib/types'
import { cn } from '@/lib/utils'

export default function NotificationsPage() {
  const qc = useQueryClient()
  const [status, setStatus] = useState('all')
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState<number | null>(null)
  const pageSize = 50
  useEffect(() => setPage(1), [status])

  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (status !== 'all') params.set('status', status)

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['notifications', page, status],
    queryFn: () => api.get<Page<Notification>>(`/notifications?${params.toString()}`),
    placeholderData: (p) => p,
  })

  const retry = useMutation({
    mutationFn: (id: number) => api.post<{ message: string }>(`/notifications/${id}/retry`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['notifications'] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / pageSize))

  return (
    <div>
      <PageHeader
        title="Benachrichtigungen"
        description="Alle versendeten Erinnerungs-E-Mails."
        actions={
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Alle</SelectItem>
              <SelectItem value="sent">Gesendet</SelectItem>
              <SelectItem value="failed">Fehlgeschlagen</SelectItem>
            </SelectContent>
          </Select>
        }
      />

      <Card className={cn('overflow-hidden', isFetching && 'opacity-70 transition-opacity')}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">Zeitpunkt</th>
                <th className="px-4 py-3 font-medium">Empfänger</th>
                <th className="px-4 py-3 font-medium">Stufe</th>
                <th className="px-4 py-3 font-medium">Backend</th>
                <th className="px-4 py-3 font-medium">Sprache</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i}>
                    <td colSpan={7} className="px-4 py-3">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      icon={Bell}
                      title="Keine Benachrichtigungen"
                      description="Es wurden noch keine Erinnerungen versendet."
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
                        <td className="max-w-[280px] truncate px-4 py-2.5">{n.recipient}</td>
                        <td className="px-4 py-2.5 whitespace-nowrap">{n.reminder_day} Tage</td>
                        <td className="text-muted-foreground px-4 py-2.5 uppercase">{n.backend}</td>
                        <td className="text-muted-foreground px-4 py-2.5 uppercase">
                          {n.language}
                        </td>
                        <td className="px-4 py-2.5">
                          <span
                            className="inline-flex items-center gap-1.5 text-xs font-medium"
                            style={{ color: ok ? 'var(--status-ok)' : 'var(--status-expired)' }}
                          >
                            <span
                              className="size-1.5 rounded-full"
                              style={{
                                background: ok ? 'var(--status-ok)' : 'var(--status-expired)',
                              }}
                            />
                            {ok ? 'Gesendet' : 'Fehler'}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          {n.status === 'failed' && (
                            <div className="flex justify-end gap-1">
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => retry.mutate(n.id)}
                                aria-label="Erneut senden"
                              >
                                <RotateCw className="size-3.5" />
                              </Button>
                              {n.error && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => setExpanded(open ? null : n.id)}
                                  aria-label="Fehler anzeigen"
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
                            colSpan={7}
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
            {data?.total ?? 0} Einträge · Seite {page}/{totalPages}
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
    </div>
  )
}
