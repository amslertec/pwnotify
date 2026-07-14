import { describe, expect, it } from 'vitest'

import { daysLeftLabel, expiryStatus } from './expiry'

describe('expiryStatus', () => {
  it('grün > 14 Tage', () => {
    expect(expiryStatus({ days_left: 20, account_enabled: true })).toBe('ok')
  })
  it('gelb 7–14 Tage', () => {
    expect(expiryStatus({ days_left: 14, account_enabled: true })).toBe('warn')
    expect(expiryStatus({ days_left: 7, account_enabled: true })).toBe('warn')
  })
  it('orange 1–6 Tage', () => {
    expect(expiryStatus({ days_left: 6, account_enabled: true })).toBe('soon')
    expect(expiryStatus({ days_left: 1, account_enabled: true })).toBe('soon')
  })
  it('rot <= 0 (abgelaufen)', () => {
    expect(expiryStatus({ days_left: 0, account_enabled: true })).toBe('expired')
    expect(expiryStatus({ days_left: -5, account_enabled: true })).toBe('expired')
  })
  it('grau = kein Ablauf', () => {
    expect(expiryStatus({ days_left: null, account_enabled: true })).toBe('never')
  })
  it('deaktivierte Konten', () => {
    expect(expiryStatus({ days_left: 3, account_enabled: false })).toBe('disabled')
  })
})

describe('daysLeftLabel', () => {
  it('formatiert Werte', () => {
    expect(daysLeftLabel(null)).toBe('—')
    expect(daysLeftLabel(0)).toBe('Heute')
    expect(daysLeftLabel(5)).toBe('5 T')
    expect(daysLeftLabel(-3)).toBe('vor 3 T')
  })
})
