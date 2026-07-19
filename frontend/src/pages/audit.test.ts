import { describe, expect, it } from 'vitest'

import de from '@/i18n/locales/de.json'
import en from '@/i18n/locales/en.json'
import i18n from '@/i18n'

import { formatiereDetail } from './audit'

/** Vollständige Liste aller Audit-Aktionskennungen aus `backend/app/services/audit.py`.
 *  Jede davon muss in de + en eine Übersetzung haben — die Zeilen-Anzeige löst JEDE
 *  Aktion auf, unabhängig davon, ob sie im `/audit/actions`-Filter-Dropdown auftaucht. */
const ALLE_AKTIONEN = [
  'auth.login_success',
  'auth.login_failed',
  'auth.login_blocked',
  'auth.logout',
  'auth.account_locked',
  'auth.password_changed',
  'auth.2fa_enabled',
  'auth.2fa_disabled',
  'auth.sessions_revoked',
  'auth.tenant_switched',
  'user.created',
  'user.deleted',
  'user.role_changed',
  'settings.changed',
  'settings.secret_changed',
  'tenant.created',
  'tenant.updated',
  'tenant.deleted',
  'tenant.assigned',
  'tenant.unassigned',
  'user.superadmin_created',
  'instance.mode_changed',
  'group.created',
  'group.updated',
  'group.deleted',
  'group.tenants_set',
  'group.synced',
  'user.invited',
  'user.invitation_accepted',
  'auth.password_reset_sent',
  'auth.password_reset_done',
  'entra_user.exclusion_changed',
  'notification.manual_send',
  'notification.retried',
  'run.triggered',
  'branding.changed',
  'user.sso_synced',
  'auth.2fa_setup_started',
]

describe('audit.actions i18n', () => {
  it('hat genau 38 bekannte Aktionskennungen (Ground Truth aus audit.py)', () => {
    expect(ALLE_AKTIONEN).toHaveLength(38)
  })

  const tDe = i18n.getFixedT('de')
  const tEn = i18n.getFixedT('en')

  it.each(ALLE_AKTIONEN)('rendert "%s" NICHT als rohen Schluessel (DE + EN)', (action) => {
    const key = `audit.actions.${action}`
    expect(tDe(key)).not.toBe(key)
    expect(tEn(key)).not.toBe(key)
  })
})

describe('audit i18n de/en Parity', () => {
  it('audit.actions: de und en haben identische Schluessel', () => {
    expect(Object.keys(de.audit.actions).sort()).toEqual(Object.keys(en.audit.actions).sort())
  })

  it('audit.detail: de und en haben identische Schluessel', () => {
    expect(Object.keys(de.audit.detail).sort()).toEqual(Object.keys(en.audit.detail).sort())
  })
})

describe('formatiereDetail', () => {
  const t = i18n.getFixedT('de')
  const tenantNamen = new Map([
    [1, 'Acme AG'],
    [2, 'Contoso GmbH'],
  ])

  it('loest tenant_ids (Liste) zu Kundennamen auf und verbindet sie', () => {
    const out = formatiereDetail({ tenant_ids: [1, 2] }, tenantNamen, t)
    expect(out).toContain('Acme AG')
    expect(out).toContain('Contoso GmbH')
    expect(out).not.toContain('tenant_ids=')
  })

  it('faellt bei unbekannter Kunden-ID im tenant_ids-Array auf #id zurueck', () => {
    const out = formatiereDetail({ tenant_ids: [99] }, tenantNamen, t)
    expect(out).toContain('#99')
  })

  it('zeigt kind als lesbares Rollen-Label statt Rohwert', () => {
    const out = formatiereDetail({ kind: 'admin' }, tenantNamen, t)
    expect(out).not.toContain('kind=admin')
    expect(out).toContain('Administrator')
  })

  it('zeigt entra_group_id lesbar mit Label statt Rohschluessel', () => {
    const out = formatiereDetail({ entra_group_id: 'grp-abc-123' }, tenantNamen, t)
    expect(out).not.toContain('entra_group_id=')
    expect(out).toContain('grp-abc-123')
  })

  it('zeigt Gruppen-Sync-Zaehler (member_count/materialized/added/removed) lesbar', () => {
    const out = formatiereDetail(
      { member_count: 12, materialized: 9, added: 2, removed: 1 },
      tenantNamen,
      t,
    )
    expect(out).not.toContain('member_count=')
    expect(out).not.toContain('materialized=')
    expect(out).not.toContain('added=')
    expect(out).not.toContain('removed=')
    expect(out).toContain('12')
    expect(out).toContain('9')
  })

  it('loest tenant_id / granted_tenant_id / home_tenant_id ueber dieselbe Kunden-Map auf', () => {
    const out = formatiereDetail(
      { tenant_id: 1, granted_tenant_id: 2, home_tenant_id: 1 },
      tenantNamen,
      t,
    )
    expect(out.match(/Acme AG/g)).toHaveLength(2)
    expect(out).toContain('Contoso GmbH')
  })

  it('zeigt sso als Ja/Nein statt true/false', () => {
    const out = formatiereDetail({ sso: true }, tenantNamen, t)
    expect(out).not.toContain('sso=true')
    expect(out).toContain('Ja')
  })

  it('faellt bei unbekannten Schluesseln unveraendert auf key=value zurueck (kein Absturz)', () => {
    expect(formatiereDetail({ reason: 'invalid_credentials' }, tenantNamen, t)).toBe(
      'reason=invalid_credentials',
    )
  })
})
