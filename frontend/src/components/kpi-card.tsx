import type { LucideIcon } from 'lucide-react'

import { Card } from './ui/card'
import { Skeleton } from './ui/skeleton'
import { cn } from '@/lib/utils'

export function KpiCard({
  label,
  value,
  icon: Icon,
  accent = 'var(--color-primary)',
  hint,
  loading,
}: {
  label: string
  value: number | string
  icon: LucideIcon
  accent?: string
  hint?: string
  loading?: boolean
}) {
  return (
    <Card className="relative overflow-hidden p-5">
      <div
        className="pointer-events-none absolute -top-6 -right-6 size-24 rounded-full opacity-[0.12] blur-xl"
        style={{ background: accent }}
      />
      <div className="flex items-start justify-between">
        <span className="text-muted-foreground text-sm font-medium">{label}</span>
        <span
          className="grid size-8 place-items-center rounded-lg"
          style={{ background: `color-mix(in srgb, ${accent} 14%, transparent)`, color: accent }}
        >
          <Icon className="size-4" />
        </span>
      </div>
      {loading ? (
        <Skeleton className="mt-3 h-9 w-20" />
      ) : (
        <div className="font-display mt-2 text-3xl font-semibold tabular-nums">{value}</div>
      )}
      {hint && <div className={cn('text-muted-foreground mt-1 text-xs')}>{hint}</div>}
    </Card>
  )
}
