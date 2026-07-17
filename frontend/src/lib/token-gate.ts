import type { TokenInfo } from './types'

/**
 * Gemeinsame Zustandslogik für die öffentlichen Token-Seiten (Einladung annehmen,
 * Passwort zurücksetzen — Task 8): kein Token in der URL, eine noch laufende Prüfung,
 * ein vom Server als ungültig gemeldeter Token (`valid=false` — niemals ein Hinweis,
 * WARUM: keine Enumeration) oder ein gültiger Token, auf genau einen von vier UI-Zuständen.
 */
export type TokenGateState = 'loading' | 'missing' | 'invalid' | 'valid'

export function resolveTokenGate(
  token: string | null,
  isLoading: boolean,
  info: TokenInfo | null,
): TokenGateState {
  if (!token) return 'missing'
  if (isLoading) return 'loading'
  if (!info || !info.valid) return 'invalid'
  return 'valid'
}
