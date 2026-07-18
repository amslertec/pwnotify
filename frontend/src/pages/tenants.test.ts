import { describe, expect, it } from 'vitest'

import { resolveTenantsTab } from './tenants'

describe('resolveTenantsTab', () => {
  it('laesst bekannte Tabs unangetastet', () => {
    expect(resolveTenantsTab('customers')).toBe('customers')
    expect(resolveTenantsTab('assignments')).toBe('assignments')
    expect(resolveTenantsTab('groups')).toBe('groups')
    expect(resolveTenantsTab('settings')).toBe('settings')
  })

  it('faellt auf den Default-Tab (customers) zurueck bei unbekanntem Wert', () => {
    expect(resolveTenantsTab('unknown')).toBe('customers')
    expect(resolveTenantsTab('')).toBe('customers')
    expect(resolveTenantsTab('general')).toBe('customers')
    // 'superadmins' ist kein Tenants-Konsolen-Tab mehr -- die Superadmin-Verwaltung lebt
    // jetzt auf der Access-Seite (superadmin-only), daher Fallback auf 'customers'.
    expect(resolveTenantsTab('superadmins')).toBe('customers')
  })
})
