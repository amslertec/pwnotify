import { useQuery } from '@tanstack/react-query'
import { createContext, useContext, useEffect, type ReactNode } from 'react'

import { api } from '@/lib/api'
import type { PublicBranding } from '@/lib/types'

const DEFAULTS: PublicBranding = {
  app_name: 'PwNotify',
  company_name: '',
  primary_color: '#4F46E5',
  reset_url: '',
  has_logo: false,
  has_favicon: false,
  logo_version: 0,
  favicon_version: 0,
}

const BrandingContext = createContext<{ branding: PublicBranding; refetch: () => void }>({
  branding: DEFAULTS,
  refetch: () => {},
})

/** Setzt --primary (und passenden Ring) zur Laufzeit aus dem Branding. */
function applyPrimary(color: string) {
  if (!color) return
  document.documentElement.style.setProperty('--primary', color)
  document.documentElement.style.setProperty('--ring', color)
}

/** Setzt bei hochgeladenem Custom-Favicon das Tab-Icon (ersetzt die Default-Favicons). */
function applyFavicon(hasCustom: boolean, version: number) {
  const id = 'pwnotify-dynamic-favicon'
  if (!hasCustom) {
    document.getElementById(id)?.remove()
    // Standard-Favicon wiederherstellen, falls keins mehr vorhanden ist.
    if (!document.querySelector('link[rel~="icon"]')) {
      const d = document.createElement('link')
      d.rel = 'icon'
      d.type = 'image/svg+xml'
      d.href = '/favicon.svg'
      document.head.appendChild(d)
    }
    return
  }
  // Default-Favicons entfernen, damit das Custom-Icon im Tab gewinnt.
  document.querySelectorAll('link[rel~="icon"]').forEach((l) => {
    if (l.id !== id) l.remove()
  })
  let link = document.getElementById(id) as HTMLLinkElement | null
  if (!link) {
    link = document.createElement('link')
    link.id = id
    link.rel = 'icon'
    document.head.appendChild(link)
  }
  link.href = `/api/branding/favicon?v=${version}`
}

export function BrandingProvider({ children }: { children: ReactNode }) {
  // Vom Inline-Bootstrap in index.html vorab geladen (sofort verfügbar, kein Flackern).
  const bootstrapped = (window as unknown as { __BRANDING__?: PublicBranding }).__BRANDING__
  const { data, refetch } = useQuery({
    queryKey: ['branding'],
    queryFn: () => api.get<PublicBranding>('/branding'),
    initialData: bootstrapped,
    staleTime: 60_000,
  })
  const branding = data ?? DEFAULTS

  useEffect(() => {
    applyPrimary(branding.primary_color)
    document.title = branding.app_name
    applyFavicon(branding.has_favicon, branding.favicon_version)
  }, [branding.primary_color, branding.app_name, branding.has_favicon, branding.favicon_version])

  return (
    <BrandingContext.Provider value={{ branding, refetch: () => void refetch() }}>
      {children}
    </BrandingContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBranding() {
  return useContext(BrandingContext)
}
