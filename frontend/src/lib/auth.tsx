import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { api, ApiError, onAuthExpired } from './api'
import type { LoginResponse, User } from './types'
import { useIdleLogout } from '@/hooks/use-idle-logout'
import { setLanguage, SUPPORTED_LANGUAGES, type Language } from '@/i18n'

/** Merkt für die Login-Seite, dass die Abmeldung wegen Inaktivität erfolgte. */
export const IDLE_LOGOUT_FLAG = 'pwnotify-idle-logout'

/** Superadmin besitzt alle Admin-Rechte (superadmin ⊇ admin). Zentrale Prüfung, damit
 *  die Access-Modell-Migration einen Superadmin nicht aus der Admin-Oberfläche aussperrt. */
export function hasAdminRights(role: string | undefined | null): boolean {
  return role === 'admin' || role === 'superadmin'
}

/** Default-Kontext (Context-Gating v2, Task 5): true nur für einen Superadmin, dessen
 *  aktiver Mandant der Standard-/Provider-Kunde ist. Ein Superadmin, der in einen
 *  Kunden-Kontext gewechselt hat (`active_tenant_is_default === false`), sieht wie
 *  jeder andere Kunden-Account KEINE provider-only Oberflächen (Konsole, Modus-Schalter,
 *  Settings-General-Tab) — siehe Design Matrix B §4. */
export function isDefaultContext(user: User | null | undefined): boolean {
  return user?.role === 'superadmin' && !!user?.active_tenant_is_default
}

interface AuthContextValue {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<LoginResponse>
  verify2fa: (code: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  switchTenant: (tenantId: number) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const qc = useQueryClient()
  const { t } = useTranslation()

  const refresh = async () => {
    try {
      setUser(await api.get<User>('/auth/me'))
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    onAuthExpired.handler = () => {
      setUser(null)
      qc.clear()
    }
    return () => {
      onAuthExpired.handler = null
    }
  }, [qc])

  // Konto-Sprache anwenden, sobald der Benutzer bekannt ist (Login/Refresh, geräteübergreifend).
  useEffect(() => {
    const lang = user?.language
    if (lang && (SUPPORTED_LANGUAGES as readonly string[]).includes(lang)) {
      setLanguage(lang as Language)
    }
  }, [user?.language])

  const login = async (username: string, password: string): Promise<LoginResponse> => {
    const res = await api.post<LoginResponse>('/auth/login', { username, password })
    if (!res.two_factor_required && res.user) setUser(res.user)
    return res
  }

  const verify2fa = async (code: string) => {
    const res = await api.post<LoginResponse>('/auth/2fa/verify', { code })
    if (res.user) setUser(res.user)
  }

  // Kundenwechsel (Multi-Tenant): der Server setzt neue Auth-Cookies für den
  // Ziel-Kunden und liefert den aktualisierten User zurück. qc.clear() erzwingt,
  // dass alle gecachten kundendaten-Queries (Dashboard, Benutzer, …) neu laden.
  const switchTenant = async (tenantId: number) => {
    try {
      const res = await api.post<User>('/auth/switch-tenant', { tenant_id: tenantId })
      setUser(res)
      qc.clear()
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        toast.error(t('tenant.switch_error'))
        return
      }
      throw e
    }
  }

  const logout = async () => {
    try {
      await api.post('/auth/logout')
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
    }
    setUser(null)
    qc.clear()
  }

  // Abmeldung bei Inaktivität. Der Server beendet untätige Sitzungen beim Token-Refresh;
  // solange ein Tab offen ist und im Hintergrund pollt, greift das aber nicht — deshalb
  // meldet der Client bei echter Untätigkeit (keine Maus-/Tastatureingabe) selbst ab.
  // Die Sitzung wird dabei serverseitig gelöscht, nicht nur der Tab geleert.
  useIdleLogout(
    user?.idle_timeout_min ?? 0,
    () => {
      sessionStorage.setItem(IDLE_LOGOUT_FLAG, '1')
      void logout()
    },
    !!user,
    // Aktivitäts-Ping: hält last_used_at auf dem Server aktuell, damit aktives Arbeiten
    // ohne API-Aufrufe (Lesen, Scrollen) nicht in den Idle-Timeout läuft. Fehler ignorieren.
    () => {
      void api.post('/auth/activity').catch(() => {})
    },
  )

  return (
    <AuthContext.Provider
      value={{ user, loading, login, verify2fa, logout, refresh, switchTenant }}
    >
      {children}
    </AuthContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth muss innerhalb von AuthProvider verwendet werden')
  return ctx
}
