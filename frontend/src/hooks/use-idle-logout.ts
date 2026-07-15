import { useEffect, useRef } from 'react'

/** Ereignisse, die als echte Nutzeraktivität zählen. Bewusst nicht: Netzwerk-Aktivität —
 *  ein offener Tab pollt im Hintergrund weiter und würde die Sitzung sonst ewig am Leben
 *  halten, obwohl niemand arbeitet. */
const ACTIVITY_EVENTS = ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'scroll']

/** Aktivität höchstens einmal pro Sekunde verarbeiten — mousemove feuert sonst dauernd. */
const THROTTLE_MS = 1000

const STORAGE_KEY = 'pwnotify-last-activity'

/**
 * Meldet nach `timeoutMin` ohne Nutzeraktivität ab.
 *
 * Über mehrere Tabs hinweg synchronisiert (localStorage): Arbeitet man in einem Tab,
 * laufen die anderen nicht in den Timeout. Der Server beendet untätige Sitzungen
 * zusätzlich beim Token-Refresh — das hier greift, während ein Tab offen bleibt.
 */
export function useIdleLogout(
  timeoutMin: number,
  onTimeout: () => void,
  enabled: boolean = true,
): void {
  // Über eine Ref, damit ein neu erzeugter Callback den Timer nicht bei jedem Render
  // zurücksetzt — sonst liefe der Timeout nie ab.
  const onTimeoutRef = useRef(onTimeout)
  useEffect(() => {
    onTimeoutRef.current = onTimeout
  }, [onTimeout])

  useEffect(() => {
    if (!enabled || timeoutMin <= 0) return

    const timeoutMs = timeoutMin * 60_000
    let timer: ReturnType<typeof setTimeout> | undefined
    let lastWrite = 0

    const stamp = () => {
      const now = Date.now()
      localStorage.setItem(STORAGE_KEY, String(now))
      return now
    }

    const arm = (from: number) => {
      if (timer) clearTimeout(timer)
      const rest = Math.max(0, from + timeoutMs - Date.now())
      timer = setTimeout(() => {
        // Beim Feuern gegen den zuletzt in IRGENDEINEM Tab gesetzten Stempel prüfen.
        const last = Number(localStorage.getItem(STORAGE_KEY) || 0)
        if (Date.now() - last >= timeoutMs) {
          onTimeoutRef.current()
        } else {
          arm(last) // woanders war Aktivität -> weiterlaufen
        }
      }, rest)
    }

    const onActivity = () => {
      const now = Date.now()
      if (now - lastWrite < THROTTLE_MS) return
      lastWrite = now
      arm(stamp())
    }

    // Rückkehr auf den Tab: Stempel prüfen, statt den Timer weiterlaufen zu lassen —
    // Browser drosseln Timer in Hintergrund-Tabs.
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      const last = Number(localStorage.getItem(STORAGE_KEY) || 0)
      if (last && Date.now() - last >= timeoutMs) onTimeoutRef.current()
      else arm(last || stamp())
    }

    for (const e of ACTIVITY_EVENTS) {
      window.addEventListener(e, onActivity, { passive: true })
    }
    document.addEventListener('visibilitychange', onVisible)

    arm(stamp())

    return () => {
      if (timer) clearTimeout(timer)
      for (const e of ACTIVITY_EVENTS) window.removeEventListener(e, onActivity)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [timeoutMin, enabled])
}
