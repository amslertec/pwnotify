import { describe, expect, it } from 'vitest'

import { renderDetailEntry } from './runs'

const translations: Record<string, string> = {
  'runs.detail.graphNotConfigured':
    'Microsoft-Graph ist nicht konfiguriert — der Benutzer-Abgleich wurde übersprungen.',
}
const t = (key: string): string => translations[key] ?? key

describe('renderDetailEntry', () => {
  it('lokalisiert den graph_not_configured-Skip statt Roh-JSON', () => {
    expect(renderDetailEntry({ step: 'sync', skipped: 'graph_not_configured' }, t)).toBe(
      'Microsoft-Graph ist nicht konfiguriert — der Benutzer-Abgleich wurde übersprungen.',
    )
  })

  it('belaesst gewoehnliche Sync-Eintraege unveraendert als JSON', () => {
    expect(renderDetailEntry({ step: 'sync', checked: 42 }, t)).toBe(
      JSON.stringify({ step: 'sync', checked: 42 }),
    )
  })

  it('belaesst unbekannte Skip-Gruende unveraendert als JSON', () => {
    const entry = { step: 'sync', skipped: 'something_else' }
    expect(renderDetailEntry(entry, t)).toBe(JSON.stringify(entry))
  })
})
