import { describe, expect, it } from 'vitest'

import {
  buildSuperadminCreatePayload,
  canSubmitSuperadminCreate,
  isSuperadminEmailValid,
  superadminCreatedToastKey,
} from './superadmins-tab'

// Hinweis (siehe `groups-tab.test.ts`/`accept-invitation.test.ts`): `vitest.config.ts` matched nur
// `src/**/*.test.ts` mit `environment: 'node'` -- kein jsdom, kein `@testing-library/react` im
// Einsatz. Deshalb testet dieses File die aus `superadmins-tab.tsx` exportierte reine Logik
// (Modus-Zweig E-Mail-only vs. Benutzername+Passwort), die `CreateSuperadminDialog` (Task 10,
// Einladungs-Parität zu `access.tsx`s `CreateDialog`) für Submit-Gate/Payload/Toast verwendet;
// das Rendering/Wiring selbst wird über `typecheck`/`build` abgesichert.

const baseForm = { firstName: '', lastName: '', username: '', password: '', email: '' }

describe('isSuperadminEmailValid', () => {
  it('akzeptiert eine plausible Adresse', () => {
    expect(isSuperadminEmailValid('alice@example.com')).toBe(true)
  })

  it('lehnt eine leere/unplausible Adresse ab', () => {
    expect(isSuperadminEmailValid('')).toBe(false)
    expect(isSuperadminEmailValid('nope')).toBe(false)
  })

  it('trimmt umgebende Leerzeichen vor der Prüfung', () => {
    expect(isSuperadminEmailValid('  alice@example.com  ')).toBe(true)
  })
})

describe('canSubmitSuperadminCreate', () => {
  it('invite-Modus: gated ausschliesslich auf eine gültige E-Mail, Benutzername/Passwort egal', () => {
    expect(canSubmitSuperadminCreate('invite', { ...baseForm, email: 'alice@example.com' })).toBe(
      true,
    )
    expect(canSubmitSuperadminCreate('invite', baseForm)).toBe(false)
  })

  it('password-Modus: braucht Benutzername (>=3) UND Passwort (>=10), E-Mail irrelevant', () => {
    expect(
      canSubmitSuperadminCreate('password', { ...baseForm, username: 'ab', password: 'x'.repeat(10) }),
    ).toBe(false)
    expect(
      canSubmitSuperadminCreate('password', {
        ...baseForm,
        username: 'abc',
        password: 'x'.repeat(9),
      }),
    ).toBe(false)
    expect(
      canSubmitSuperadminCreate('password', {
        ...baseForm,
        username: 'abc',
        password: 'x'.repeat(10),
      }),
    ).toBe(true)
  })
})

describe('buildSuperadminCreatePayload', () => {
  it('invite-Modus: postet AUSSCHLIESSLICH die getrimmte E-Mail -- kein username/password', () => {
    const payload = buildSuperadminCreatePayload('invite', {
      ...baseForm,
      username: 'should-be-ignored',
      password: 'should-be-ignored',
      email: '  alice@example.com  ',
    })
    expect(payload).toEqual({ email: 'alice@example.com' })
  })

  it('password-Modus: postet username/password/display_name, kein email-Feld', () => {
    const payload = buildSuperadminCreatePayload('password', {
      firstName: 'Alice',
      lastName: 'Admin',
      username: 'alice-admin',
      password: 'a-strong-password-1',
      email: 'ignored@example.com',
    })
    expect(payload).toEqual({
      username: 'alice-admin',
      password: 'a-strong-password-1',
      display_name: 'Alice Admin',
    })
  })

  it('password-Modus: display_name wird null, wenn Vor-/Nachname leer bleiben', () => {
    const payload = buildSuperadminCreatePayload('password', {
      ...baseForm,
      username: 'alice-admin',
      password: 'a-strong-password-1',
    })
    expect(payload).toEqual({
      username: 'alice-admin',
      password: 'a-strong-password-1',
      display_name: null,
    })
  })
})

describe('superadminCreatedToastKey', () => {
  it('invite-Modus -> die geteilte "Einladung gesendet"-Toast-Kopie aus access.*', () => {
    expect(superadminCreatedToastKey('invite')).toBe('access.inviteSent')
  })

  it('password-Modus -> die bestehende Superadmin-Erstellt-Toast-Kopie', () => {
    expect(superadminCreatedToastKey('password')).toBe('tenants.superadmins.created')
  })
})
