import { Lock, LockOpen } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Input } from '../ui/input'
import { cn } from '@/lib/utils'

interface Props {
  value: string
  onChange: (v: string) => void
  /** War beim Laden bereits ein Wert gespeichert? Dann startet das Feld gesperrt. */
  hasSavedValue: boolean
  /** Zählt bei jedem erfolgreichen Speichern hoch — sperrt das Feld danach wieder. */
  lockSignal: number
  /** Nur Administratoren dürfen entsperren. Auditoren erreichen die Seite ohnehin nicht,
   *  aber der Schutz wird hier bewusst zusätzlich durchgesetzt. */
  canUnlock: boolean
  type?: string
  placeholder?: string
  className?: string
  /** Für Mehrfeld-Layouts (z. B. col-span). */
  wrapperClassName?: string
}

/**
 * Eingabefeld, das nach dem Speichern gesperrt ist. Ein Schloss-Button rechts entsperrt
 * es zum Bearbeiten; nach dem nächsten Speichern sperrt es wieder. Schützt Verbindungs-
 * und Zugangsdaten (Graph, SSO, Mail) vor versehentlicher Änderung.
 */
export function LockableInput({
  value,
  onChange,
  hasSavedValue,
  lockSignal,
  canUnlock,
  type,
  placeholder,
  className,
  wrapperClassName,
}: Props) {
  const { t } = useTranslation()
  const [locked, setLocked] = useState(hasSavedValue)
  const firstSignal = useRef(true)

  // Nach jedem Speichern (lockSignal steigt) wieder sperren — den allerersten Lauf
  // überspringen, sonst würde ein leeres, noch nie gespeichertes Feld sofort sperren.
  useEffect(() => {
    if (firstSignal.current) {
      firstSignal.current = false
      return
    }
    setLocked(true)
  }, [lockSignal])

  return (
    <div className={cn('relative', wrapperClassName)}>
      <Input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        readOnly={locked}
        aria-readonly={locked}
        className={cn(locked && 'bg-muted/50 text-muted-foreground cursor-default pr-10', className)}
      />
      {locked
        ? canUnlock && (
            <button
              type="button"
              onClick={() => setLocked(false)}
              className="text-muted-foreground hover:text-foreground focus-visible:ring-ring absolute top-1/2 right-2 -translate-y-1/2 rounded p-1 focus-visible:ring-2 focus-visible:outline-none"
              aria-label={t('settingsPage.unlock')}
              title={t('settingsPage.unlock')}
            >
              <Lock className="size-4" />
            </button>
          )
        : hasSavedValue && (
            <button
              type="button"
              onClick={() => setLocked(true)}
              className="text-primary hover:text-primary/80 focus-visible:ring-ring absolute top-1/2 right-2 -translate-y-1/2 rounded p-1 focus-visible:ring-2 focus-visible:outline-none"
              aria-label={t('settingsPage.lock')}
              title={t('settingsPage.lock')}
            >
              <LockOpen className="size-4" />
            </button>
          )}
    </div>
  )
}
