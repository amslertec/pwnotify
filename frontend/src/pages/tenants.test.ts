import { describe, expect, it } from 'vitest'

import { resolveTenantsTab } from './tenants'

describe('resolveTenantsTab', () => {
  it('laesst bekannte Tabs unangetastet', () => {
    expect(resolveTenantsTab('customers')).toBe('customers')
    expect(resolveTenantsTab('assignments')).toBe('assignments')
    expect(resolveTenantsTab('groups')).toBe('groups')
    expect(resolveTenantsTab('superadmins')).toBe('superadmins')
    expect(resolveTenantsTab('settings')).toBe('settings')
  })

  it('faellt auf den Default-Tab (customers) zurueck bei unbekanntem Wert', () => {
    expect(resolveTenantsTab('unknown')).toBe('customers')
    expect(resolveTenantsTab('')).toBe('customers')
    expect(resolveTenantsTab('general')).toBe('customers')
  })
})
