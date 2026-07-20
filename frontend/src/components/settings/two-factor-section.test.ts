import { describe, expect, it } from 'vitest'

import { canDisable } from './two-factor-section'

// Pure logic, no DOM (repo convention -- vitest.config.ts only matches `.test.ts`, no
// jsdom). Tests the gating predicate of the disable form: per L1, the password must be set
// alongside the code, otherwise the disable button must not fire.

describe('canDisable', () => {
  it('allows disabling with code AND password', () => {
    expect(canDisable({ code: '123456', password: 'Str0ng!Pass99' })).toBe(true)
  })

  it('blocks when the password is missing (L1: reauth required)', () => {
    expect(canDisable({ code: '123456', password: '' })).toBe(false)
  })

  it('blocks when the code is missing', () => {
    expect(canDisable({ code: '', password: 'Str0ng!Pass99' })).toBe(false)
  })

  it('blocks on a whitespace-only code (no false green)', () => {
    expect(canDisable({ code: '   ', password: 'Str0ng!Pass99' })).toBe(false)
  })
})
