import { describe, expect, it } from 'vitest'

import { adminAvatarSrc, resetGate, showSsoTab } from './access'

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

describe('adminAvatarSrc', () => {
  it('baut die versionierte Admin-Avatar-URL, wenn has_avatar gesetzt ist', () => {
    expect(adminAvatarSrc({ id: 7, has_avatar: true, avatar_version: 42 })).toBe(
      '/api/admin/users/7/avatar?v=42',
    )
  })

  it('liefert undefined ohne has_avatar -- AvatarImage faellt dann auf Initialen zurueck', () => {
    expect(adminAvatarSrc({ id: 7, has_avatar: false, avatar_version: 0 })).toBeUndefined()
  })
})

describe('showSsoTab', () => {
  it('zeigt den SSO-Tab im Single-Tenant-Modus (wie bisher)', () => {
    expect(showSsoTab(false)).toBe(true)
  })

  it('versteckt den SSO-Tab im Multi-Tenant-Modus -- SSO laeuft dort ueber Teams', () => {
    expect(showSsoTab(true)).toBe(false)
  })
})
