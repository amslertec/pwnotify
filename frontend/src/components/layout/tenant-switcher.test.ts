import { describe, expect, it } from 'vitest'

import { isSwitcherVisible } from './tenant-switcher'
import type { User } from '@/lib/types'

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: 'u',
    display_name: null,
    is_sso: false,
    role: 'superadmin',
    language: 'de',
    two_factor_enabled: false,
    last_login_at: null,
    has_avatar: false,
    avatar_version: 0,
    idle_timeout_min: 0,
    active_tenant: { id: 1, name: 'Default' },
    switchable_tenants: [
      { id: 1, name: 'Default' },
      { id: 2, name: 'Kunde A' },
    ],
    multi_tenant_mode: true,
    active_tenant_is_default: true,
    ...overrides,
  }
}

describe('isSwitcherVisible', () => {
  it('sichtbar fuer einen Superadmin im Default-Kontext mit mehreren Kunden', () => {
    expect(isSwitcherVisible(makeUser())).toBe(true)
  })

  it('bleibt sichtbar in einem Kunden-Kontext (Superadmin hat umgeschaltet) -- Rueckweg muss erhalten bleiben', () => {
    expect(
      isSwitcherVisible(
        makeUser({ active_tenant_is_default: false, active_tenant: { id: 2, name: 'Kunde A' } }),
      ),
    ).toBe(true)
  })

  it('unsichtbar, wenn die Mandantenfaehigkeit instanzweit aus ist', () => {
    expect(isSwitcherVisible(makeUser({ multi_tenant_mode: false }))).toBe(false)
  })

  it('unsichtbar, wenn das Konto nur einen (oder keinen) Kunden hat', () => {
    expect(isSwitcherVisible(makeUser({ switchable_tenants: [{ id: 1, name: 'Default' }] }))).toBe(
      false,
    )
    expect(isSwitcherVisible(makeUser({ switchable_tenants: [] }))).toBe(false)
  })

  it('unsichtbar fuer null/undefined', () => {
    expect(isSwitcherVisible(null)).toBe(false)
    expect(isSwitcherVisible(undefined)).toBe(false)
  })
})
