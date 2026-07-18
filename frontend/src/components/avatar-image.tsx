import { useEffect, useState } from 'react'

import { cn, initials } from '@/lib/utils'

/**
 * Entscheidet rein anhand von `src` und dem Fehler-Flag, ob ein Foto oder die
 * Initialen gerendert werden sollen. Als eigene Funktion exportiert, damit sie
 * ohne DOM/JSX getestet werden kann.
 */
export function resolveAvatarView(src: string | undefined, errored: boolean): 'image' | 'initials' {
  if (src && !errored) return 'image'
  return 'initials'
}

/** Wiederverwendbares Profilbild — zeigt `src`, fällt bei fehlendem/kaputtem Bild auf Initialen zurück. */
export function AvatarImage({
  name,
  src,
  className,
}: {
  name: string
  src?: string
  className?: string
}) {
  const [errored, setErrored] = useState(false)

  // Bei Wechsel des Bildes (z. B. anderer Nutzer) erneut versuchen.
  useEffect(() => {
    setErrored(false)
  }, [src])

  if (resolveAvatarView(src, errored) === 'image') {
    return (
      <img
        src={src}
        alt={name}
        className={cn('rounded-full object-cover', className)}
        onError={() => setErrored(true)}
      />
    )
  }

  return (
    <span
      className={cn(
        'bg-primary/15 text-primary grid place-items-center rounded-full font-semibold',
        className,
      )}
    >
      {initials(name)}
    </span>
  )
}
