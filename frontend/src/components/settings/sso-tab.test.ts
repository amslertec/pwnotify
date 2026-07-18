import { describe, expect, it } from 'vitest'

import { buildSsoSavePayload, showRoleGroupFields } from './sso-tab'

describe('showRoleGroupFields', () => {
  it('zeigt die Rollen-Gruppen-Felder im Single-Tenant-Modus', () => {
    expect(showRoleGroupFields(false)).toBe(true)
  })

  it('versteckt die Rollen-Gruppen-Felder im Multi-Tenant-Modus', () => {
    expect(showRoleGroupFields(true)).toBe(false)
  })
})

describe('buildSsoSavePayload', () => {
  const values = {
    enabled: true,
    groupId: 'admin-group-id',
    auditorGroupId: ' auditor-group-id ',
    label: 'Mit Microsoft anmelden',
    publicUrl: 'https://domain.example.com/',
  }

  it('enthaelt die Gruppen-Keys im Single-Tenant-Modus (wie bisher)', () => {
    expect(buildSsoSavePayload(values, false)).toEqual({
      'oidc.enabled': true,
      'oidc.admin_group_id': 'admin-group-id',
      'oidc.auditor_group_id': 'auditor-group-id',
      'oidc.button_label': 'Mit Microsoft anmelden',
      'app.public_url': 'https://domain.example.com',
    })
  })

  it('laesst die Gruppen-Keys im Multi-Tenant-Modus komplett weg -- kein Blank-Save', () => {
    const payload = buildSsoSavePayload(values, true)
    expect(payload).toEqual({
      'oidc.enabled': true,
      'oidc.button_label': 'Mit Microsoft anmelden',
      'app.public_url': 'https://domain.example.com',
    })
    expect(payload).not.toHaveProperty('oidc.admin_group_id')
    expect(payload).not.toHaveProperty('oidc.auditor_group_id')
  })
})
