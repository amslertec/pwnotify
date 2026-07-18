import { describe, expect, it } from 'vitest'

import { resolveTab, showGeneralTab, showSsoTab } from './settings'
import type { User } from '@/lib/types'

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

describe('showGeneralTab', () => {
  it('versteckt fuer einen normalen Admin', () => {
    expect(showGeneralTab(makeUser({ role: 'admin', active_tenant_is_default: true }))).toBe(false)
  })

  it('versteckt fuer einen Superadmin im Kunden-Kontext', () => {
    expect(showGeneralTab(makeUser({ role: 'superadmin', active_tenant_is_default: false }))).toBe(
      false,
    )
  })

  it('zeigt fuer einen Superadmin im Default-Kontext', () => {
    expect(showGeneralTab(makeUser({ role: 'superadmin', active_tenant_is_default: true }))).toBe(
      true,
    )
  })

  it('versteckt fuer null/undefined', () => {
    expect(showGeneralTab(null)).toBe(false)
    expect(showGeneralTab(undefined)).toBe(false)
  })
})

describe('showSsoTab', () => {
  it('zeigt im Single-Tenant-Betrieb fuer jeden Benutzer', () => {
    expect(showSsoTab(makeUser({ multi_tenant_mode: false, role: 'admin' }))).toBe(true)
    expect(
      showSsoTab(makeUser({ multi_tenant_mode: false, role: 'superadmin', active_tenant_is_default: false })),
    ).toBe(true)
  })

  it('versteckt im Mandantenmodus fuer einen Nicht-Default-Kontext', () => {
    expect(
      showSsoTab(
        makeUser({ multi_tenant_mode: true, role: 'superadmin', active_tenant_is_default: false }),
      ),
    ).toBe(false)
    expect(
      showSsoTab(makeUser({ multi_tenant_mode: true, role: 'admin', active_tenant_is_default: true })),
    ).toBe(false)
  })

  it('zeigt im Mandantenmodus nur im Default-/Provider-Kontext eines Superadmins', () => {
    expect(
      showSsoTab(
        makeUser({ multi_tenant_mode: true, role: 'superadmin', active_tenant_is_default: true }),
      ),
    ).toBe(true)
  })

  it('zeigt fuer null/undefined (Fallback: kein Mandantenmodus)', () => {
    expect(showSsoTab(null)).toBe(true)
    expect(showSsoTab(undefined)).toBe(true)
  })
})

describe('resolveTab', () => {
  it('laesst andere Tabs unangetastet', () => {
    expect(resolveTab('graph', false, true)).toBe('graph')
    expect(resolveTab('mail', true, true)).toBe('mail')
  })

  it('faellt von general auf graph zurueck, wenn General nicht sichtbar ist', () => {
    expect(resolveTab('general', false, true)).toBe('graph')
  })

  it('behaelt general, wenn es sichtbar ist', () => {
    expect(resolveTab('general', true, true)).toBe('general')
  })

  it('faellt von sso auf graph zurueck, wenn SSO nicht sichtbar ist', () => {
    expect(resolveTab('sso', true, false)).toBe('graph')
  })

  it('behaelt sso, wenn es sichtbar ist', () => {
    expect(resolveTab('sso', true, true)).toBe('sso')
  })
})
