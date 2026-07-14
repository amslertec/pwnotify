import { expiryStatus, STATUS_META } from '@/lib/expiry'
import type { EntraUser } from '@/lib/types'

/**
 * Signature-Element: Ablauf-Ring im Logo-Motiv (Uhr-Ring = Zeit).
 * Der Arc füllt sich, je näher der Ablauf rückt; Farbe folgt der Status-Skala.
 */
export function ExpiryRing({
  user,
  size = 44,
  stroke = 4,
  window = 30,
}: {
  user: Pick<EntraUser, 'days_left' | 'account_enabled'>
  size?: number
  stroke?: number
  window?: number
}) {
  const status = expiryStatus(user)
  const color = STATUS_META[status].varName
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r

  // Fraktion des Rings: 0 (weit weg / kein Ablauf) .. 1 (abgelaufen)
  let fraction = 0
  if (user.days_left != null) {
    const clamped = Math.max(0, Math.min(window, user.days_left))
    fraction = 1 - clamped / window
    if (user.days_left <= 0) fraction = 1
  }
  const dash = c * fraction

  const label = user.days_left == null ? '∞' : `${user.days_left}`

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="var(--color-muted)"
        strokeWidth={stroke}
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${dash} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: 'stroke-dasharray 0.6s ease' }}
      />
      <text
        x="50%"
        y="50%"
        dominantBaseline="central"
        textAnchor="middle"
        className="font-display font-semibold tabular-nums"
        style={{ fontSize: size * 0.3, fill: color }}
      >
        {label}
      </text>
    </svg>
  )
}
