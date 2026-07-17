import { describe, expect, it } from 'vitest'

import { resolveTokenGate } from './token-gate'
import type { TokenInfo } from './types'

describe('resolveTokenGate', () => {
  it('fehlt der Token in der URL -> "missing" (keine Anfrage noetig)', () => {
    expect(resolveTokenGate(null, false, null)).toBe('missing')
    expect(resolveTokenGate('', false, null)).toBe('missing')
  })

  it('Token vorhanden, Anfrage laeuft noch -> "loading"', () => {
    expect(resolveTokenGate('tok', true, null)).toBe('loading')
  })

  it('Server meldet valid=false -> "invalid" (keine Enumeration, kein Detailgrund)', () => {
    const info: TokenInfo = { valid: false, email: null, purpose: null }
    expect(resolveTokenGate('tok', false, info)).toBe('invalid')
  })

  it('keine Antwort (z. B. Query-Fehler) -> "invalid"', () => {
    expect(resolveTokenGate('tok', false, null)).toBe('invalid')
  })

  it('Server meldet valid=true -> "valid"', () => {
    const info: TokenInfo = { valid: true, email: 'a@b.de', purpose: 'invite' }
    expect(resolveTokenGate('tok', false, info)).toBe('valid')
  })
})
