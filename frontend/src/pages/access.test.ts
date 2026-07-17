import { describe, expect, it } from 'vitest'

import { resetGate } from './access'

describe('resetGate', () => {
  it('sperrt ein pending-Konto (noch nicht aktiv) unabhaengig von der E-Mail', () => {
    expect(resetGate({ is_active: false, is_sso: false, email: null })).toEqual({
      disabled: true,
      hint: 'pending',
    })
    expect(resetGate({ is_active: false, is_sso: false, email: 'a@b.ch' })).toEqual({
      disabled: true,
      hint: 'pending',
    })
  })

  it('sperrt ein aktives lokales Konto ohne E-Mail proaktiv (noEmail)', () => {
    expect(resetGate({ is_active: true, is_sso: false, email: null })).toEqual({
      disabled: true,
      hint: 'noEmail',
    })
  })

  it('laesst ein aktives lokales Konto MIT E-Mail zu', () => {
    expect(resetGate({ is_active: true, is_sso: false, email: 'a@b.ch' })).toEqual({
      disabled: false,
      hint: null,
    })
  })

  it('gated ein SSO-Konto NIE auf die lokale E-Mail-Regel', () => {
    expect(resetGate({ is_active: true, is_sso: true, email: null })).toEqual({
      disabled: false,
      hint: null,
    })
  })
})
