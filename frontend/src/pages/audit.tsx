import { useQuery } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { ScrollText, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { EmptyState } from '@/components/empty-state'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
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
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/format'
import type { AuditEntry, AuditPage, Tenant } from '@/lib/types'

const PAGE_SIZE = 25
const ALLE = '__all__'

/** Zeitraum-Optionen. Der Standard zeigt bewusst alles — bei einem Vorfall sucht man
 *  zuerst und filtert dann, nicht umgekehrt. */
const ZEITRAEUME = [
  { value: ALLE, key: 'audit.range.all' },
  { value: '1', key: 'audit.range.day' },
  { value: '7', key: 'audit.range.week' },
  { value: '30', key: 'audit.range.month' },
  { value: '90', key: 'audit.range.quarter' },
]

export default function AuditPage() {
  const { t } = useTranslation()
  const [page, setPage] = useState(1)
  const [action, setAction] = useState(ALLE)
  const [outcome, setOutcome] = useState(ALLE)
  const [days, setDays] = useState(ALLE)

  const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
  if (action !== ALLE) params.set('action', action)
  if (outcome !== ALLE) params.set('outcome', outcome)
  if (days !== ALLE) params.set('days', days)

  const { data, isLoading } = useQuery({
    queryKey: ['audit', page, action, outcome, days],
    queryFn: () => api.get<AuditPage>(`/audit?${params.toString()}`),
    placeholderData: (p) => p,
  })

  const { data: actions } = useQuery({
    queryKey: ['audit-actions'],
    queryFn: () => api.get<string[]>('/audit/actions'),
  })

  // Für die Detail-Spalte: `tenant_id=4` ist für Menschen bedeutungslos, der Kundenname
  // nicht. Fehler beim Laden (z. B. keine Admin-Rechte) dürfen die Audit-Seite nicht zum
  // Absturz bringen — dann bleibt die Map einfach leer und es wird auf `#id` zurückgefallen.
  const { data: tenants } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: () => api.get<Tenant[]>('/admin/tenants'),
    throwOnError: false,
  })
  const tenantNamen = new Map((tenants ?? []).map((tn) => [tn.id, tn.name]))

  const rows = data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))

  // Filterwechsel darf nicht auf einer leeren Seite landen.
  const setzeFilter = (fn: () => void) => {
    fn()
    setPage(1)
  }

  return (
    <div>
      <PageHeader title={t('audit.title')} description={t('audit.description')} />

      <Card className="overflow-hidden">
        <div className="border-border flex flex-wrap gap-2 border-b p-4">
          <Select value={action} onValueChange={(v) => setzeFilter(() => setAction(v))}>
            <SelectTrigger className="w-full sm:w-64">
              <SelectValue placeholder={t('audit.filter.action')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALLE}>{t('audit.filter.allActions')}</SelectItem>
              {(actions ?? []).map((a) => (
                <SelectItem key={a} value={a}>
                  {t(`audit.actions.${a}`, { defaultValue: a })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={outcome} onValueChange={(v) => setzeFilter(() => setOutcome(v))}>
            <SelectTrigger className="w-full sm:w-44">
              <SelectValue placeholder={t('audit.filter.outcome')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALLE}>{t('audit.filter.allOutcomes')}</SelectItem>
              <SelectItem value="success">{t('audit.outcome.success')}</SelectItem>
              <SelectItem value="failure">{t('audit.outcome.failure')}</SelectItem>
            </SelectContent>
          </Select>

          <Select value={days} onValueChange={(v) => setzeFilter(() => setDays(v))}>
            <SelectTrigger className="w-full sm:w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ZEITRAEUME.map((z) => (
                <SelectItem key={z.value} value={z.value}>
                  {t(z.key)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-sm">
            <thead>
              <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                <th className="px-4 py-3 font-medium">{t('audit.columns.time')}</th>
                <th className="px-4 py-3 font-medium">{t('audit.columns.actor')}</th>
                <th className="px-4 py-3 font-medium">{t('audit.columns.action')}</th>
                <th className="px-4 py-3 font-medium">{t('audit.columns.target')}</th>
                <th className="px-4 py-3 font-medium">{t('audit.columns.ip')}</th>
                <th className="px-4 py-3 font-medium">{t('audit.columns.detail')}</th>
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
                      icon={ScrollText}
                      title={t('audit.empty.title')}
                      description={t('audit.empty.description')}
                    />
                  </td>
                </tr>
              ) : (
                rows.map((e) => <AuditRow key={e.id} entry={e} tenantNamen={tenantNamen} />)
              )}
            </tbody>
          </table>
        </div>

        <div className="border-border flex items-center justify-between border-t px-4 py-3 text-sm">
          <span className="text-muted-foreground">
            {t('audit.pagination.summary', { count: data?.total ?? 0, page, total: totalPages })}
          </span>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              {t('audit.pagination.prev')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              {t('audit.pagination.next')}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}

function AuditRow({
  entry,
  tenantNamen,
}: {
  entry: AuditEntry
  tenantNamen: Map<number, string>
}) {
  const { t } = useTranslation()
  const fehler = entry.outcome === 'failure'
  const Icon = fehler ? ShieldAlert : ShieldCheck

  return (
    <tr className="hover:bg-muted/40">
      <td className="text-muted-foreground px-4 py-3 whitespace-nowrap">
        {fmtDateTime(entry.at)}
      </td>
      <td className="px-4 py-3">
        {entry.actor_username ?? (
          <span className="text-muted-foreground italic">{t('audit.actor.system')}</span>
        )}
      </td>
      <td className="px-4 py-3">
        <span className="flex items-center gap-2">
          <Icon className={fehler ? 'text-destructive size-4' : 'text-muted-foreground size-4'} />
          <span>{t(`audit.actions.${entry.action}`, { defaultValue: entry.action })}</span>
          {fehler && (
            <Badge variant="danger" className="text-[10px]">
              {t('audit.outcome.failure')}
            </Badge>
          )}
        </span>
      </td>
      <td className="text-muted-foreground px-4 py-3">{entry.target ?? '—'}</td>
      <td className="text-muted-foreground px-4 py-3 font-mono text-xs">
        {entry.ip_address ?? '—'}
      </td>
      <td className="text-muted-foreground px-4 py-3">
        {Object.keys(entry.detail).length === 0 ? (
          '—'
        ) : (
          <span className="font-mono text-xs">
            {formatiereDetail(entry.detail, tenantNamen, t)}
          </span>
        )}
      </td>
    </tr>
  )
}

/** Kompakt und lesbar: `schluessel=wert`, Listen zusammengezogen. Sonderfall `tenant_id`:
 *  wird zu einem lesbaren Kundennamen aufgelöst (Map kann leer sein — Fallback auf `#id`,
 *  etwa wenn der Kunde inzwischen gelöscht wurde oder `/admin/tenants` noch lädt). */
function formatiereDetail(
  detail: Record<string, unknown>,
  tenantNamen: Map<number, string>,
  t: TFunction,
): string {
  return Object.entries(detail)
    .map(([k, v]) => {
      if (k === 'tenant_id') {
        const id = Number(v)
        const name = tenantNamen.get(id)
        return `${t('audit.detail.tenant')}: ${name ?? `#${id}`}`
      }
      return `${k}=${Array.isArray(v) ? v.join(', ') : String(v)}`
    })
    .join('  ·  ')
}
