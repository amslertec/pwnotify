import { describe, expect, it } from 'vitest'

import { canSubmitReset, classifyResetError } from './reset-password'

// Testansatz wie `accept-invitation.test.ts` -- reine Logik, kein DOM (`vitest.config.ts`
// matched ohnehin nur `.test.ts`, kein jsdom im Repo; s. cg2-task-6-report.md).

describe('canSubmitReset', () => {
  it('erlaubt Absenden bei gueltigem, uebereinstimmendem Passwort', () => {
    expect(canSubmitReset({ password: 'Str0ng!Pass99', confirm: 'Str0ng!Pass99' })).toBe(true)
  })

  it('sperrt bei nicht uebereinstimmender Bestaetigung', () => {
    expect(canSubmitReset({ password: 'Str0ng!Pass99', confirm: 'AndersStr0ng!99' })).toBe(false)
  })

  it('sperrt, wenn das Passwort die Policy nicht erfuellt', () => {
    expect(canSubmitReset({ password: 'short', confirm: 'short' })).toBe(false)
  })

  it('sperrt bei zwei leeren Feldern (kein falsches Gruen)', () => {
    expect(canSubmitReset({ password: '', confirm: '' })).toBe(false)
  })
})

describe('classifyResetError', () => {
  it('erkennt token_invalid -> Wechsel auf die Invalid-Ansicht', () => {
    expect(classifyResetError('token_invalid')).toBe('invalid')
  })

  it('faellt fuer alles andere (z. B. password_policy) auf "other" (Toast) zurueck', () => {
    expect(classifyResetError('password_policy')).toBe('other')
    expect(classifyResetError(undefined)).toBe('other')
  })
})
