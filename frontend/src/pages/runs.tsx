import { useQuery } from '@tanstack/react-query'
import { ChevronDown, History } from 'lucide-react'
import { Fragment, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { RunStatusPill } from '@/components/run-status'
import { api } from '@/lib/api'
import { fmtDateTime, fmtDuration, fmtRelative } from '@/lib/format'
import type { Page, Run, RunDetail } from '@/lib/types'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 10

export default function RunsPage() {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [openId, setOpenId] = useState<number | null>(null)

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['runs', page],
    queryFn: () => api.get<Page<Run>>(`/runs?page=${page}&page_size=${PAGE_SIZE}`),
    placeholderData: (p) => p,
  })

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))

  return (
    <div>
      <PageHeader title={t('runs.title')} description={t('runs.description')} />

      <Card className={cn('overflow-hidden', isFetching && 'opacity-70 transition-opacity')}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs tracking-wide uppercase">
                <th className="w-8 py-3 pl-4" />
                <th className="px-4 py-3 font-medium">{t('runs.columns.time')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.status')}</th>
                <th className="px-4 py-3 font-medium">{t('runs.columns.trigger')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('runs.columns.checked')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('runs.columns.sent')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('runs.columns.failed')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('runs.columns.duration')}</th>
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
            {t('runs.pagination.summary', { count: data?.total ?? 0, page, total: totalPages })}
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

/** Zahl rechtsbündig; 0 wird zum dezenten „–", damit echte Werte hervorstechen. */
function Metric({ value, tone }: { value: number; tone?: 'ok' | 'danger' }) {
  if (!value) return <td className="text-muted-foreground/50 px-4 py-3 text-right">–</td>
  const color =
    tone === 'ok' ? 'var(--status-ok)' : tone === 'danger' ? 'var(--status-expired)' : undefined
  return (
    <td className="px-4 py-3 text-right font-medium tabular-nums" style={color ? { color } : undefined}>
      {value}
    </td>
  )
}

function RunRow({ run, open, onToggle }: { run: Run; open: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  const { data: detail } = useQuery({
    queryKey: ['run', run.id],
    queryFn: () => api.get<RunDetail>(`/runs/${run.id}`),
    enabled: open,
  })

  const triggerLabel =
    run.trigger === 'manual' ? t('runs.trigger.manual') : t('runs.trigger.scheduled')

  return (
    <Fragment>
      <tr className="hover:bg-muted/30 cursor-pointer" onClick={onToggle}>
        <td className="py-3 pl-4">
          <ChevronDown
            className={cn(
              'text-muted-foreground size-4 transition-transform',
              open && 'rotate-180',
            )}
          />
        </td>
        <td className="px-4 py-3 whitespace-nowrap">
          <div>{fmtDateTime(run.started_at)}</div>
          <div className="text-muted-foreground text-xs">{fmtRelative(run.started_at)}</div>
        </td>
        <td className="px-4 py-3">
          <RunStatusPill status={run.status} />
        </td>
        <td className="text-muted-foreground px-4 py-3 whitespace-nowrap">
          {triggerLabel}
          {run.dry_run && (
            <>
              {' · '}
              <span className="text-foreground/70">{t('runs.dryRunBadge')}</span>
            </>
          )}
        </td>
        <Metric value={run.checked_users} />
        <Metric value={run.sent} tone="ok" />
        <Metric value={run.failed} tone="danger" />
        <td className="text-muted-foreground px-4 py-3 text-right whitespace-nowrap tabular-nums">
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
