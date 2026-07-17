import { describe, expect, it } from 'vitest'

import { visibleNavItems } from './sidebar'
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

function paths(user: User | null | undefined): string[] {
  return visibleNavItems(user).map((item) => item.to)
}

describe('visibleNavItems', () => {
  it('Auditor sieht nur die vier Basis-Eintraege', () => {
    expect(paths(makeUser({ role: 'auditor' }))).toEqual(['/', '/users', '/notifications', '/runs'])
  })

  it('Admin sieht zusaetzlich Access/Audit/Settings, aber nicht Tenants', () => {
    const items = paths(makeUser({ role: 'admin' }))
    expect(items).toContain('/access')
    expect(items).toContain('/audit')
    expect(items).toContain('/settings')
    expect(items).not.toContain('/tenants')
  })

  it('Superadmin im Default-Kontext (Mode an) sieht /tenants', () => {
    const items = paths(
      makeUser({ role: 'superadmin', multi_tenant_mode: true, active_tenant_is_default: true }),
    )
    expect(items).toContain('/tenants')
  })

  it('Superadmin im Kunden-Kontext (nach Umschalten) sieht /tenants NICHT', () => {
    const items = paths(
      makeUser({ role: 'superadmin', multi_tenant_mode: true, active_tenant_is_default: false }),
    )
    expect(items).not.toContain('/tenants')
  })

  it('Mandantenfaehigkeit aus (Default) -> /tenants unsichtbar, auch fuer Superadmin im Default-Kontext', () => {
    const items = paths(
      makeUser({ role: 'superadmin', multi_tenant_mode: false, active_tenant_is_default: true }),
    )
    expect(items).not.toContain('/tenants')
  })
})
