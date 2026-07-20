import { describe, expect, it } from 'vitest'

import { hasAdminRights, isDefaultContext, shouldRevalidateSession } from './auth'
import type { User } from './types'

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: 'u',
    display_name: null,
    is_sso: false,
    role: 'admin',
    language: 'de',
    two_factor_enabled: false,
    last_login_at: null,
    has_avatar: false,
    avatar_version: 0,
    idle_timeout_min: 0,
    active_tenant: null,
    switchable_tenants: [],
    multi_tenant_mode: false,
    active_tenant_is_default: false,
    email: null,
    ...overrides,
  }
}

describe('hasAdminRights', () => {
  it('admin und superadmin haben Admin-Rechte', () => {
    expect(hasAdminRights('admin')).toBe(true)
    expect(hasAdminRights('superadmin')).toBe(true)
  })
  it('auditor und undefined haben keine Admin-Rechte', () => {
    expect(hasAdminRights('auditor')).toBe(false)
    expect(hasAdminRights(undefined)).toBe(false)
    expect(hasAdminRights(null)).toBe(false)
  })
})

describe('isDefaultContext', () => {
  it('true fuer Superadmin im Default-Kontext', () => {
    expect(isDefaultContext(makeUser({ role: 'superadmin', active_tenant_is_default: true }))).toBe(
      true,
    )
  })
  it('false fuer Superadmin, der in einen Kunden-Kontext gewechselt hat', () => {
    expect(
      isDefaultContext(makeUser({ role: 'superadmin', active_tenant_is_default: false })),
    ).toBe(false)
  })
  it('false fuer Admin, auch mit active_tenant_is_default=true', () => {
    expect(isDefaultContext(makeUser({ role: 'admin', active_tenant_is_default: true }))).toBe(
      false,
    )
  })
  it('false fuer null/undefined', () => {
    expect(isDefaultContext(null)).toBe(false)
    expect(isDefaultContext(undefined)).toBe(false)
  })
})

describe('shouldRevalidateSession', () => {
  it('revalidiert, wenn Tab sichtbar, Nutzer eingeloggt und kein Call laeuft (FE1)', () => {
    expect(shouldRevalidateSession('visible', true, false)).toBe(true)
  })
  it('revalidiert NICHT bei verstecktem Tab (visibilitychange -> hidden)', () => {
    expect(shouldRevalidateSession('hidden', true, false)).toBe(false)
  })
  it('revalidiert NICHT ohne eingeloggten Nutzer (Login-Seite -> kein /auth/me)', () => {
    expect(shouldRevalidateSession('visible', false, false)).toBe(false)
  })
  it('revalidiert NICHT, wenn bereits ein Call laeuft (visibilitychange + focus gleichzeitig)', () => {
    expect(shouldRevalidateSession('visible', true, true)).toBe(false)
  })
})
