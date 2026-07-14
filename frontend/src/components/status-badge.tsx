import { useTranslation } from 'react-i18next'

import { expiryStatus, STATUS_META, type ExpiryStatus } from '@/lib/expiry'
import type { EntraUser } from '@/lib/types'
import { cn } from '@/lib/utils'

export function StatusDot({ status, className }: { status: ExpiryStatus; className?: string }) {
  return (
    <span
      className={cn('inline-block size-2 rounded-full', className)}
      style={{ background: STATUS_META[status].varName }}
    />
  )
}

/** Farbcodiertes Badge für „verbleibende Tage". */
export function DaysBadge({ user }: { user: Pick<EntraUser, 'days_left' | 'account_enabled'> }) {
  const { t } = useTranslation()
  const status = expiryStatus(user)
  const color = STATUS_META[status].varName
  const text =
    user.days_left == null
      ? '—'
      : user.days_left <= 0
        ? t('statusBadge.daysOver', { n: Math.abs(user.days_left) })
        : t('statusBadge.days', { n: user.days_left })

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums"
      style={{
        color,
        background: `color-mix(in srgb, ${color} 14%, transparent)`,
      }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {text}
    </span>
  )
}

export function StatusLabel({ status }: { status: ExpiryStatus }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      <StatusDot status={status} />
      {STATUS_META[status].label}
    </span>
  )
}
