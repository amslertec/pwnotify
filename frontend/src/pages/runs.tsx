import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, History, Play } from 'lucide-react'
import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { BackendStatusBar, RunStatusPill } from '@/components/backend-status'
import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { translateError } from '@/lib/errors'
import { fmtDateTime, fmtDuration } from '@/lib/format'
import type { Page, Run, RunDetail } from '@/lib/types'
import { cn } from '@/lib/utils'

export default function RunsPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [page, setPage] = useState(1)
  const [openId, setOpenId] = useState<number | null>(null)
  const pageSize = 25

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['runs', page],
    queryFn: () => api.get<Page<Run>>(`/runs?page=${page}&page_size=${pageSize}`),
    placeholderData: (p) => p,
  })

  const trigger = useMutation({
    mutationFn: (dryRun: boolean) => api.post<RunDetail>('/runs/trigger', { dry_run: dryRun }),
    onSuccess: (run) => {
      toast.success(t('runs.toast.completed', { count: run.sent }))
      void qc.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / pageSize))

  return (
    <div>
      <PageHeader
        title={t('runs.title')}
        description={t('runs.description')}
        actions={
          <>
            <Button
              variant="outline"
              onClick={() => trigger.mutate(true)}
              loading={trigger.isPending}
            >
              {t('runs.actions.dryRun')}
            </Button>
            <Button onClick={() => trigger.mutate(false)} loading={trigger.isPending}>
              <Play /> {t('runs.actions.runNow')}
            </Button>
          </>
        }
      />

      <div className="mb-4">
        <BackendStatusBar />
      </div>

      <Card className={cn('overflow-hidden', isFetching && 'opacity-70 transition-opacity')}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="w-8 px-4 py-3" />
                <th className="px-4 py-3 font-medium">{t('runs.columns.time')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.status')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.trigger')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.checked')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.sent')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.failed')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.duration')}</th>
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {isLoading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i}>
                    <td colSpan={8} className="px-4 py-3">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={8}>
                    <EmptyState
                      icon={History}
                      title={t('runs.empty.title')}
                      description={t('runs.empty.description')}
                    />
                  </td>
                </tr>
              ) : (
                rows.map((r) => (
                  <RunRow
                    key={r.id}
                    run={r}
                    open={openId === r.id}
                    onToggle={() => setOpenId((o) => (o === r.id ? null : r.id))}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="border-border flex items-center justify-between border-t px-4 py-3 text-sm">
          <span className="text-muted-foreground">
            {t('runs.pagination.summary', {
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
              {t('runs.pagination.prev')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              {t('runs.pagination.next')}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}

function RunRow({ run, open, onToggle }: { run: Run; open: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  const { data: detail } = useQuery({
    queryKey: ['run', run.id],
    queryFn: () => api.get<RunDetail>(`/runs/${run.id}`),
    enabled: open,
  })

  return (
    <Fragment>
      <tr className="hover:bg-muted/30 cursor-pointer" onClick={onToggle}>
        <td className="px-4 py-2.5">
          <ChevronDown
            className={cn(
              'text-muted-foreground size-4 transition-transform',
              open && 'rotate-180',
            )}
          />
        </td>
        <td className="px-4 py-2.5 whitespace-nowrap">{fmtDateTime(run.started_at)}</td>
        <td className="px-4 py-2.5">
          <div className="flex items-center gap-1.5">
            <RunStatusPill status={run.status} />
            {run.dry_run && <Badge variant="secondary">{t('runs.dryRunBadge')}</Badge>}
          </div>
        </td>
        <td className="text-muted-foreground px-4 py-2.5">
          {run.trigger === 'manual' ? t('runs.trigger.manual') : t('runs.trigger.scheduled')}
        </td>
        <td className="px-4 py-2.5 tabular-nums">{run.checked_users}</td>
        <td className="px-4 py-2.5 text-[color:var(--status-ok)] tabular-nums">{run.sent}</td>
        <td
          className="px-4 py-2.5 tabular-nums"
          style={run.failed ? { color: 'var(--status-expired)' } : undefined}
        >
          {run.failed}
        </td>
        <td className="text-muted-foreground px-4 py-2.5 whitespace-nowrap">
          {fmtDuration(run.duration_ms)}
        </td>
      </tr>
      {open && (
        <tr className="bg-muted/20">
          <td colSpan={8} className="px-4 py-3">
            {run.error && (
              <p className="text-danger mb-2 font-mono text-xs break-words">{run.error}</p>
            )}
            {detail ? (
              detail.detail_log.length > 0 ? (
                <div className="max-h-72 space-y-1 overflow-auto">
                  {detail.detail_log.map((entry, i) => (
                    <div key={i} className="text-muted-foreground font-mono text-xs break-words">
                      {JSON.stringify(entry)}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-muted-foreground text-xs">{t('runs.noDetailLog')}</p>
              )
            ) : (
              <Skeleton className="h-16 w-full" />
            )}
          </td>
        </tr>
      )}
    </Fragment>
  )
}
