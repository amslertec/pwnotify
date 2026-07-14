import { Globe } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { setLanguage, SUPPORTED_LANGUAGES, type Language } from '@/i18n'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { cn } from '@/lib/utils'

/** DE/EN-Umschalter unten in der Sidebar. Persistiert die Sprache am Konto und
 *  wechselt sofort (ohne Reload). */
export function LanguageSwitcher({ collapsed }: { collapsed: boolean }) {
  const { i18n, t } = useTranslation()
  const { refresh } = useAuth()
  const current = (i18n.resolvedLanguage || 'de') as Language

  const change = async (lang: Language) => {
    if (lang === current) return
    setLanguage(lang) // sofort sichtbar
    try {
      await api.post('/auth/language', { language: lang })
      await refresh() // Konto-Sprache aktualisieren (geräteübergreifend)
    } catch {
      /* Sprache bleibt zumindest lokal gesetzt */
    }
  }

  if (collapsed) {
    const next: Language = current === 'de' ? 'en' : 'de'
    return (
      <div className="border-sidebar-border border-t p-3">
        <button
          onClick={() => change(next)}
          className="text-muted-foreground hover:text-foreground hover:bg-muted/70 mx-auto flex size-9 items-center justify-center rounded-lg text-xs font-semibold uppercase transition-colors"
          aria-label={t('language.label')}
          title={t('language.label')}
        >
          {current}
        </button>
      </div>
    )
  }

  return (
    <div className="border-sidebar-border space-y-2 border-t p-3">
      <div className="text-muted-foreground flex items-center gap-2 px-1 text-xs font-medium">
        <Globe className="size-3.5" /> {t('language.label')}
      </div>
      <div className="bg-muted/50 flex gap-1 rounded-lg p-1">
        {SUPPORTED_LANGUAGES.map((lng) => (
          <button
            key={lng}
            onClick={() => change(lng)}
            className={cn(
              'flex-1 rounded-md px-2 py-1.5 text-xs font-medium transition-colors',
              current === lng
                ? 'bg-card text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {t(`language.${lng}`)}
          </button>
        ))}
      </div>
    </div>
  )
}
