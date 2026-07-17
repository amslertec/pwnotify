import type { ReactNode } from 'react'

import { Logo } from '@/components/logo'

/** Gemeinsame Karten-/Seitenhülle für öffentliche, unauthentifizierte Formulare
 *  (Einladung annehmen, Passwort zurücksetzen — Task 8). Spiegelt die
 *  Logo-über-Karte-Anordnung von `setup.tsx`, nur einspaltig statt Stepper. */
export function PublicAuthLayout({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: ReactNode
}) {
  return (
    <div className="bg-muted/30 flex min-h-full items-center justify-center py-10">
      <div className="w-full max-w-md px-4">
        <div className="mb-6 flex flex-col items-center text-center">
          <Logo />
          <h1 className="font-display mt-4 text-2xl font-semibold">{title}</h1>
          {subtitle && <p className="text-muted-foreground mt-1 text-sm">{subtitle}</p>}
        </div>
        <div className="border-border bg-card rounded-xl border p-6 shadow-sm">{children}</div>
      </div>
    </div>
  )
}
