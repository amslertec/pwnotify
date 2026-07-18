import { describe, expect, it } from 'vitest'

import { resolveTenantsTab } from './tenants'

describe('resolveTenantsTab', () => {
  it('laesst bekannte Tabs unangetastet', () => {
    expect(resolveTenantsTab('customers')).toBe('customers')
    expect(resolveTenantsTab('assignments')).toBe('assignments')
    expect(resolveTenantsTab('groups')).toBe('groups')
  })

  it('faellt auf den Default-Tab (customers) zurueck bei unbekanntem Wert', () => {
    expect(resolveTenantsTab('unknown')).toBe('customers')
    expect(resolveTenantsTab('')).toBe('customers')
    expect(resolveTenantsTab('general')).toBe('customers')
    // Weder 'superadmins' (jetzt auf der Access-Seite) noch 'settings' (der redundante
    // Verweis-Tab wurde entfernt -- Instanz-Einstellungen leben unter /settings) sind
    // noch Tenants-Konsolen-Tabs, daher Fallback auf 'customers'.
    expect(resolveTenantsTab('superadmins')).toBe('customers')
    expect(resolveTenantsTab('settings')).toBe('customers')
  })
})
