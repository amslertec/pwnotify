import { describe, expect, it } from 'vitest'

import { canDisable } from './two-factor-section'

// Reine Logik, kein DOM (Repo-Konvention -- vitest.config.ts matched nur `.test.ts`, kein
// jsdom). Getestet wird das Freigabe-Prädikat des Disable-Formulars: nach L1 muss neben dem
// Code auch das Passwort gesetzt sein, sonst darf der Deaktivieren-Button nicht auslösen.

describe('canDisable', () => {
  it('erlaubt Deaktivieren bei Code UND Passwort', () => {
    expect(canDisable({ code: '123456', password: 'Str0ng!Pass99' })).toBe(true)
  })

  it('sperrt, wenn das Passwort fehlt (L1: Reauth erforderlich)', () => {
    expect(canDisable({ code: '123456', password: '' })).toBe(false)
  })

  it('sperrt, wenn der Code fehlt', () => {
    expect(canDisable({ code: '', password: 'Str0ng!Pass99' })).toBe(false)
  })

  it('sperrt bei reinem Whitespace-Code (kein falsches Gruen)', () => {
    expect(canDisable({ code: '   ', password: 'Str0ng!Pass99' })).toBe(false)
  })
})
