import { describe, expect, it } from 'vitest'

import { canSubmitAccept, classifyAcceptError, type AcceptFormState } from './accept-invitation'

// Hinweis (siehe `lib/password.test.ts` / cg2-task-6-report.md): `frontend/vitest.config.ts`
// matched nur `src/**/*.test.ts` mit `environment: 'node'` -- kein jsdom, kein
// `@testing-library/react` im Einsatz. Ein `.test.tsx`-Render-Test würde vom `pnpm run
// test`-Glob gar nicht erfasst. Deshalb testet dieses File wie alle anderen Page-Tests im
// Repo (`settings.test.ts`, `tenants.test.ts`) die aus der Seite exportierte reine Logik;
// das Rendering/Wiring selbst wird über `typecheck`/`build` abgesichert.

function makeForm(overrides: Partial<AcceptFormState> = {}): AcceptFormState {
  return {
    firstName: 'Ada',
    lastName: 'Lovelace',
    username: 'ada',
    password: 'Str0ng!Pass99',
    confirm: 'Str0ng!Pass99',
    ...overrides,
  }
}

describe('canSubmitAccept', () => {
  it('erlaubt Absenden, wenn alle Bedingungen erfüllt sind', () => {
    expect(canSubmitAccept(makeForm())).toBe(true)
  })

  it('sperrt bei nicht übereinstimmendem Passwort/Bestätigung', () => {
    expect(canSubmitAccept(makeForm({ confirm: 'AndersStr0ng!99' }))).toBe(false)
  })

  it('sperrt bei leerem Vor- oder Nachname', () => {
    expect(canSubmitAccept(makeForm({ firstName: '' }))).toBe(false)
    expect(canSubmitAccept(makeForm({ lastName: '   ' }))).toBe(false)
  })

  it('sperrt bei zu kurzem Benutzernamen (< 3 Zeichen)', () => {
    expect(canSubmitAccept(makeForm({ username: 'ab' }))).toBe(false)
    expect(canSubmitAccept(makeForm({ username: 'abc' }))).toBe(true)
  })

  it('sperrt, wenn das Passwort die Policy nicht erfüllt', () => {
    expect(canSubmitAccept(makeForm({ password: 'short', confirm: 'short' }))).toBe(false)
  })

  it('sperrt bei leerem Formular', () => {
    expect(
      canSubmitAccept({ firstName: '', lastName: '', username: '', password: '', confirm: '' }),
    ).toBe(false)
  })
})

describe('classifyAcceptError', () => {
  it('erkennt username_taken -> Feldfehler, Formular bleibt erhalten', () => {
    expect(classifyAcceptError('username_taken')).toBe('username_taken')
  })

  it('erkennt token_invalid -> Wechsel auf die Invalid-Ansicht', () => {
    expect(classifyAcceptError('token_invalid')).toBe('invalid')
  })

  it('faellt fuer alles andere (z. B. password_policy) auf "other" (Toast) zurueck', () => {
    expect(classifyAcceptError('password_policy')).toBe('other')
    expect(classifyAcceptError(undefined)).toBe('other')
  })
})
