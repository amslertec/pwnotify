/**
 * Typisierter API-Client. Cookies (httpOnly) tragen die Auth; bei 401 wird genau
 * einmal ein Refresh versucht (single-flight) und der Request wiederholt.
 */

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message)
  }
}

const BASE = '/api'

// Endpunkte, deren 401 NICHT über einen automatischen Refresh + Retry laufen darf -- sie
// ETABLIEREN die Session selbst, ein verschachtelter Refresh wäre eine Endlosschleife. Alles
// andere (inkl. `/auth/me`) DARF refreshen -- so überlebt ein Bootstrap-`/auth/me` nach
// abgelaufenem 15-Min-Access-Token, statt die noch gültige 14-Tage-Refresh-Session zu verwerfen.
// `/auth/2fa/verify` (nicht das breite `/auth/2fa`!) -- nur der Login-2FA-Schritt etabliert
// die Session; die 2FA-VERWALTUNG (`/auth/2fa/setup|enable|disable`) ist ein normaler
// authentifizierter Call und DARF wie `/auth/me` refreshen. `/auth/logout` braucht keinen
// Refresh (die Sitzung wird ohnehin verworfen).
const NO_AUTO_REFRESH = ['/auth/refresh', '/auth/login', '/auth/2fa/verify', '/auth/logout']

// Idle-Flag-Key -- muss mit `IDLE_LOGOUT_FLAG` in `auth.tsx` übereinstimmen (hier als Literal,
// weil `auth.tsx` `api.ts` importiert und ein Rück-Import einen Zyklus erzeugte).
const IDLE_LOGOUT_FLAG = 'pwnotify-idle-logout'

let refreshPromise: Promise<boolean> | null = null

/** Führt `fn` TAB-ÜBERGREIFEND serialisiert aus (Web Locks API). Ohne die API (ältere Browser)
 *  Fallback auf direkte Ausführung -- dann greift nur der In-Tab-Single-Flight (`refreshPromise`). */
function withCrossTabLock<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined
  if (!locks?.request) return fn()
  return locks.request(name, fn) as Promise<T>
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshPromise) {
    // CROSS-TAB-SERIALISIERUNG: nur EIN Tab refresht gleichzeitig; die anderen warten und
    // senden danach das bereits rotierte (geteilte) Cookie. Ohne das sendet ein zweiter Tab
    // an der 15-Min-Grenze noch das eben verbrauchte Refresh-Token -> der Server wertet den
    // Hash-Mismatch als Token-Diebstahl und widerruft die GANZE Sitzung (`revoke_all`) ->
    // stiller Logout in allen Tabs (die gemeldete Ursache).
    refreshPromise = withCrossTabLock('pwnotify-token-refresh', async () => {
      const r = await fetch(`${BASE}/auth/refresh`, { method: 'POST', credentials: 'include' })
      if (!r.ok) {
        // Ein SERVER-seitiger Idle-Logout soll dieselbe Meldung zeigen wie der Client-Timer
        // (sonst wäre er stumm) -- das Flag liest die Login-Seite aus.
        try {
          const data = await r.json()
          if (data?.error?.code === 'session_idle_timeout') {
            sessionStorage.setItem(IDLE_LOGOUT_FLAG, '1')
          }
        } catch {
          /* kein JSON */
        }
      }
      return r.ok
    })
      .catch(() => false)
      .finally(() => {
        // Promise nach kurzem Tick freigeben, damit parallele In-Tab-Aufrufe teilen
        setTimeout(() => (refreshPromise = null), 0)
      })
  }
  return refreshPromise
}

/** Wird ausgelöst, wenn auch der Refresh scheitert -> App leitet zum Login. */
export const onAuthExpired = { handler: null as null | (() => void) }

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  isRetry = false,
  raw = false,
): Promise<T> {
  // no-store: Browser (v. a. Opera) darf API-Antworten nicht cachen -> immer frisch.
  const opts: RequestInit = { method, credentials: 'include', headers: {}, cache: 'no-store' }
  if (body !== undefined) {
    ;(opts.headers as Record<string, string>)['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, opts)

  if (res.status === 401 && !isRetry && !NO_AUTO_REFRESH.some((p) => path.startsWith(p))) {
    const ok = await tryRefresh()
    if (ok) return request<T>(method, path, body, true, raw)
    onAuthExpired.handler?.()
    throw new ApiError(401, 'token_expired', 'Sitzung abgelaufen.')
  }

  if (!res.ok) {
    let code = 'error'
    let message = res.statusText
    try {
      const data = await res.json()
      code = data?.error?.code ?? code
      message = data?.error?.message ?? message
    } catch {
      /* kein JSON */
    }
    throw new ApiError(res.status, code, message)
  }

  if (raw) return res as unknown as T
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string, body?: unknown) => request<T>('DELETE', path, body),
  raw: (path: string) => request<Response>('GET', path, undefined, false, true),
}

/** Multipart-Upload (Logo/Favicon). */
export async function uploadFile(path: string, file: File): Promise<void> {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch(`${BASE}${path}`, { method: 'POST', credentials: 'include', body: fd })
  if (!res.ok) {
    let message = 'Upload fehlgeschlagen.'
    try {
      const data = await res.json()
      message = data?.error?.message ?? message
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, 'upload_error', message)
  }
}
