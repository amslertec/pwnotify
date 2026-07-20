import { describe, expect, it } from 'vitest'

import { isTestModeEnabled } from './general-tab'

// Pure logic, no DOM (repo convention -- vitest.config.ts only matches `.test.ts`, no jsdom).
// The predicate drives the test-mode switch's initial state; a false green would silently arm
// real sends to disabled + unlicensed accounts, so undefined/missing must read as off.

describe('isTestModeEnabled', () => {
  it('is true when the setting is true', () => {
    expect(isTestModeEnabled({ 'sync.test_mode': true })).toBe(true)
  })

  it('is false when the setting is false', () => {
    expect(isTestModeEnabled({ 'sync.test_mode': false })).toBe(false)
  })

  it('defaults to false when the setting is missing', () => {
    expect(isTestModeEnabled({})).toBe(false)
  })
})
