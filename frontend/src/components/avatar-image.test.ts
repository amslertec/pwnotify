import { describe, expect, it } from 'vitest'

import { resolveAvatarView } from './avatar-image'

// Testansatz wie `reset-password.test.ts` -- reine Logik, kein DOM.

describe('resolveAvatarView', () => {
  it('zeigt das Bild, wenn eine src vorhanden ist und kein Fehler aufgetreten ist', () => {
    expect(resolveAvatarView('/api/auth/me/avatar?v=1', false)).toBe('image')
  })

  it('faellt auf Initialen zurueck, wenn keine src vorhanden ist', () => {
    expect(resolveAvatarView(undefined, false)).toBe('initials')
  })

  it('faellt auf Initialen zurueck, wenn das Bild einen Fehler geworfen hat (404/kaputt)', () => {
    expect(resolveAvatarView('/api/auth/me/avatar?v=1', true)).toBe('initials')
  })

  it('faellt auf Initialen zurueck, wenn weder src vorhanden ist noch (irrelevant) errored gesetzt ist', () => {
    expect(resolveAvatarView(undefined, true)).toBe('initials')
  })
})
