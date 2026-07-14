import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  CalendarClock,
  Infinity as InfinityIcon,
  Send,
  UserX,
  Users as UsersIcon,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useTheme } from '@/components/theme-provider'
import { DaysBadge } from '@/components/status-badge'
import { KpiCard } from '@/components/kpi-card'
import { PageHeader } from '@/components/page-header'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { fmtDate } from '@/lib/format'
import type { DashboardData } from '@/lib/types'
import { initials } from '@/lib/utils'

const STATUS_COLORS: Record<string, string> = {
  ok: 'var(--status-ok)',
  soon: 'var(--status-warn)',
  expired: 'var(--status-expired)',
  never: 'var(--status-never)',
  disabled: 'var(--status-soon)',
}
const STATUS_LABELS: Record<string, string> = {
  ok: 'Aktiv & gültig',
  soon: 'Läuft bald ab',
  expired: 'Abgelaufen',
  never: 'Kein Ablauf',
  disabled: 'Deaktiviert',
}

export default function DashboardPage() {
  const { resolved } = useTheme()
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardData>('/dashboard'),
    refetchInterval: 30_000,
  })

  const axis = resolved === 'dark' ? '#94a3b8' : '#64748b'
  const k = data?.kpis
  const total = k?.total ?? 0
  const pct = (n: number) => (total ? `${Math.round((n / total) * 100)} % der Benutzer` : undefined)
  const dist = (data?.status_distribution ?? []).filter((d) => d.count > 0)

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description="Überblick über Passwort-Ablauf und Benachrichtigungen."
      />

      {/* KPI-Karten */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 xl:grid-cols-6">
        <KpiCard label="Benutzer gesamt" value={total} icon={UsersIcon} loading={isLoading} />
        <KpiCard
          label="Ablauf ≤ 7 Tage"
          value={k?.expiring_soon ?? 0}
          icon={CalendarClock}
          accent="var(--status-soon)"
          hint={pct(k?.expiring_soon ?? 0)}
          loading={isLoading}
        />
        <KpiCard
          label="Abgelaufen"
          value={k?.expired ?? 0}
          icon={AlertTriangle}
          accent="var(--status-expired)"
          hint={(k?.expired ?? 0) > 0 ? 'Handeln erforderlich' : 'Alles aktuell'}
          loading={isLoading}
        />
        <KpiCard
          label="Kein Ablauf"
          value={k?.never ?? 0}
          icon={InfinityIcon}
          accent="var(--status-never)"
          hint={pct(k?.never ?? 0)}
          loading={isLoading}
        />
        <KpiCard
          label="Deaktiviert"
          value={k?.disabled ?? 0}
          icon={UserX}
          accent="var(--status-soon)"
          loading={isLoading}
        />
        <KpiCard
          label="Mails heute"
          value={k?.mails_today ?? 0}
          icon={Send}
          accent="var(--color-accent)"
          loading={isLoading}
        />
      </div>

      {/* Charts */}
      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Ablauf-Verteilung — nächste 30 Tage</CardTitle>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-56 w-full" />
            ) : (
              <ResponsiveContainer width="100%" height={224}>
                <BarChart data={data?.expiry_histogram ?? []} margin={{ left: -20, right: 8 }}>
                  <XAxis
                    dataKey="date"
                    tickFormatter={(d: string) => fmtDate(d).slice(0, 5)}
                    tick={{ fontSize: 11, fill: axis }}
                    interval={4}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    allowDecimals={false}
                    tick={{ fontSize: 11, fill: axis }}
                    axisLine={false}
                    tickLine={false}
                    width={28}
                  />
                  <RTooltip
                    cursor={{ fill: 'var(--color-muted)' }}
                    contentStyle={{
                      background: 'var(--color-popover)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    labelFormatter={(d) => fmtDate(d as string)}
                    formatter={(v) => [v, 'Abläufe']}
                  />
                  <Bar
                    dataKey="count"
                    fill="var(--color-primary)"
                    radius={[4, 4, 0, 0]}
                    maxBarSize={22}
                  />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        {/* Status-Verteilung */}
        <Card>
          <CardHeader>
            <CardTitle>Status-Verteilung</CardTitle>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <Skeleton className="h-56 w-full" />
            ) : (
              <>
                <div className="relative">
                  <ResponsiveContainer width="100%" height={168}>
                    <PieChart>
                      <Pie
                        data={dist}
                        dataKey="count"
                        nameKey="status"
                        innerRadius={54}
                        outerRadius={80}
                        paddingAngle={2}
                        strokeWidth={0}
                      >
                        {dist.map((d) => (
                          <Cell
                            key={d.status}
                            fill={STATUS_COLORS[d.status] ?? 'var(--status-never)'}
                          />
                        ))}
                      </Pie>
                      <RTooltip
                        contentStyle={{
                          background: 'var(--color-popover)',
                          border: '1px solid var(--color-border)',
                          borderRadius: 8,
                          fontSize: 12,
                        }}
                        formatter={(v, n) => [v, STATUS_LABELS[n as string] ?? n]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                    <span className="font-display text-2xl font-semibold tabular-nums">
                      {total}
                    </span>
                    <span className="text-muted-foreground text-xs">Benutzer</span>
                  </div>
                </div>
                <div className="mt-3 space-y-1.5">
                  {(data?.status_distribution ?? []).map((d) => (
                    <div key={d.status} className="flex items-center gap-2 text-xs">
                      <span
                        className="size-2.5 rounded-full"
                        style={{ background: STATUS_COLORS[d.status] }}
                      />
                      <span className="text-muted-foreground">{STATUS_LABELS[d.status]}</span>
                      <span className="ml-auto font-medium tabular-nums">{d.count}</span>
                      <span className="text-muted-foreground w-9 text-right tabular-nums">
                        {total ? Math.round((d.count / total) * 100) : 0} %
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Top 10 */}
      <Card className="mt-4">
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Nächste Ablaufdaten</CardTitle>
          <Button variant="ghost" size="sm" asChild>
            <Link to="/users">Alle Benutzer</Link>
          </Button>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : (data?.top_upcoming.length ?? 0) === 0 ? (
            <p className="text-muted-foreground py-6 text-center text-sm">
              Noch keine Daten — starten Sie einen Sync auf der Benutzer-Seite.
            </p>
          ) : (
            <div className="divide-border divide-y">
              {data?.top_upcoming.map((u) => (
                <div key={u.id} className="flex items-center gap-3 py-2.5">
                  <span className="bg-primary/10 text-primary grid size-8 shrink-0 place-items-center rounded-full text-xs font-semibold">
                    {initials(u.display_name)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{u.display_name}</p>
                    <p className="text-muted-foreground truncate font-mono text-xs">{u.upn}</p>
                  </div>
                  <span className="text-muted-foreground hidden text-xs sm:block">
                    {fmtDate(u.expiry_date)}
                  </span>
                  <DaysBadge user={u} />
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
