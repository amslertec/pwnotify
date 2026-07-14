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

let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = fetch(`${BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        // Promise nach kurzem Tick freigeben, damit parallele Aufrufe teilen
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

  if (res.status === 401 && !isRetry && !path.startsWith('/auth/')) {
    const ok = await tryRefresh()
    if (ok) return request<T>(method, path, body, true, raw)
    onAuthExpired.handler?.()
    throw new ApiError(401, 'unauthorized', 'Sitzung abgelaufen.')
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
