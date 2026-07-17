import { describe, expect, it } from 'vitest'

import { checkPassword, passwordsMatch, passwordValid } from './password'

describe('checkPassword', () => {
  it('konformes Passwort erfüllt alle fünf Regeln', () => {
    expect(checkPassword('Abcdefg1!')).toEqual({
      length: false, // nur 9 Zeichen
      upper: true,
      lower: true,
      digit: true,
      special: true,
    })
    expect(checkPassword('Abcdefgh1!')).toEqual({
      length: true, // 10 Zeichen
      upper: true,
      lower: true,
      digit: true,
      special: true,
    })
  })

  it('zu kurzes Passwort: length=false, Rest ok', () => {
    const rules = checkPassword('Abcd1!go')
    expect(rules.length).toBe(false)
  })

  it('ohne Grossbuchstaben: upper=false', () => {
    const rules = checkPassword('abcdefgh1!')
    expect(rules.upper).toBe(false)
  })

  it('ohne Kleinbuchstaben: lower=false', () => {
    const rules = checkPassword('ABCDEFGH1!')
    expect(rules.lower).toBe(false)
  })

  it('ohne Ziffer: digit=false', () => {
    const rules = checkPassword('Abcdefgh!!')
    expect(rules.digit).toBe(false)
  })

  it('ohne Sonderzeichen: special=false', () => {
    const rules = checkPassword('Abcdefgh12')
    expect(rules.special).toBe(false)
  })

  it('leerer String: alles false', () => {
    expect(checkPassword('')).toEqual({
      length: false,
      upper: false,
      lower: false,
      digit: false,
      special: false,
    })
  })
})

describe('passwordValid', () => {
  it('true bei voll konformem Passwort', () => {
    expect(passwordValid('Abcdefgh1!')).toBe(true)
  })

  it('false wenn nur eine Regel fehlt (Länge)', () => {
    expect(passwordValid('Abcdef1!')).toBe(false)
  })

  it('false wenn Sonderzeichen fehlt', () => {
    expect(passwordValid('Abcdefgh12')).toBe(false)
  })

  it('false bei leerem String', () => {
    expect(passwordValid('')).toBe(false)
  })
})

describe('passwordsMatch', () => {
  it('true bei identischen, nicht-leeren Strings', () => {
    expect(passwordsMatch('geheim123!', 'geheim123!')).toBe(true)
  })

  it('false bei unterschiedlichen Strings', () => {
    expect(passwordsMatch('geheim123!', 'anders123!')).toBe(false)
  })

  it('false wenn beide leer sind (kein Match-Signal für leeres Feld)', () => {
    expect(passwordsMatch('', '')).toBe(false)
  })
})
