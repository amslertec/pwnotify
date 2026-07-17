import { describe, expect, it } from 'vitest'

import { resolveTab, showGeneralTab } from './settings'
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

describe('resolveTab', () => {
  it('laesst andere Tabs unangetastet', () => {
    expect(resolveTab('graph', false)).toBe('graph')
    expect(resolveTab('mail', true)).toBe('mail')
  })

  it('faellt von general auf graph zurueck, wenn General nicht sichtbar ist', () => {
    expect(resolveTab('general', false)).toBe('graph')
  })

  it('behaelt general, wenn es sichtbar ist', () => {
    expect(resolveTab('general', true)).toBe('general')
  })
})
