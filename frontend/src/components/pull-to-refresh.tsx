import { RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { cn } from '@/lib/utils'

const THRESHOLD = 70 // ab hier löst das Loslassen den Reload aus
const MAX = 110 // maximaler Zugweg
const RESISTANCE = 0.5 // gedämpftes Mitziehen

/** Läuft die App als installierte PWA? Nur dort fehlt ein Reload-Button. */
function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

/** Pull-to-Refresh für die installierte PWA. Wrappt den scrollbaren Hauptbereich;
 *  im Browser bewusst inaktiv (dort gibt es Reload-Button / natives Pull-to-Refresh). */
export function PullToRefresh({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const ref = useRef<HTMLElement>(null)
  const startY = useRef<number | null>(null)
  const distRef = useRef(0)
  const [dist, setDist] = useState(0)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el || !isStandalone()) return

    const set = (v: number) => {
      distRef.current = v
      setDist(v)
    }

    const onStart = (e: TouchEvent) => {
      startY.current = el.scrollTop <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null
    }

    const onMove = (e: TouchEvent) => {
      if (startY.current === null || refreshing) return
      const delta = e.touches[0].clientY - startY.current
      if (delta <= 0) {
        set(0)
        return
      }
      if (el.scrollTop > 0) {
        startY.current = null
        set(0)
        return
      }
      e.preventDefault() // Overscroll unterdrücken, solange wir ziehen
      set(Math.min(MAX, delta * RESISTANCE))
    }

    const onEnd = () => {
      if (startY.current === null) return
      startY.current = null
      const d = distRef.current
      set(0)
      if (d >= THRESHOLD) {
        setRefreshing(true)
        window.location.reload()
      }
    }

    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchmove', onMove, { passive: false })
    el.addEventListener('touchend', onEnd)
    el.addEventListener('touchcancel', onEnd)
    return () => {
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchmove', onMove)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onEnd)
    }
  }, [refreshing])

  const active = dist > 0 || refreshing
  const label = refreshing
    ? t('pull.refreshing')
    : dist >= THRESHOLD
      ? t('pull.release')
      : t('pull.idle')

  return (
    <main ref={ref} className="relative flex-1 overflow-y-auto overscroll-y-contain">
      {active && (
        <div
          className="text-muted-foreground pointer-events-none absolute inset-x-0 top-0 z-10 flex flex-col items-center justify-end gap-1 overflow-hidden"
          style={{ height: Math.max(dist, refreshing ? 56 : 0), opacity: Math.min(1, dist / 40) || 1 }}
        >
          <RefreshCw
            className={cn('size-5', refreshing && 'animate-spin')}
            style={refreshing ? undefined : { transform: `rotate(${dist * 3}deg)` }}
          />
          <span className="text-xs">{label}</span>
        </div>
      )}
      <div
        style={{
          transform: `translateY(${refreshing ? 56 : dist}px)`,
          transition: dist > 0 ? 'none' : 'transform 200ms ease-out',
        }}
      >
        {children}
      </div>
    </main>
  )
}
