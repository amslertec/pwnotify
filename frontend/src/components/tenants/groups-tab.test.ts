import { describe, expect, it } from 'vitest'

import {
  groupMembersPath,
  groupMembersQueryKey,
  groupSyncPath,
  hasNeverSynced,
  isLastMembersPage,
  memberDisplayName,
  MEMBERS_PAGE_SIZE,
  syncToastParams,
} from './groups-tab'

// Hinweis (siehe `accept-invitation.test.ts` / `lib/password.test.ts`): `vitest.config.ts`
// matched nur `src/**/*.test.ts` mit `environment: 'node'` -- kein jsdom, kein
// `@testing-library/react` im Einsatz. Deshalb testet dieses File die aus `groups-tab.tsx`
// exportierte reine Logik (Query-Keys, Endpoint-Pfade, Pagination-Prädikate, Toast-Mapping),
// die die Komponente für Expand/Pagination/Sync verwendet; das Rendering/Wiring selbst wird
// über `typecheck`/`build` abgesichert.

describe('groupMembersPath', () => {
  it('baut den Members-Endpoint mit fixer size=25 (Server liefert 422 ab size > 200)', () => {
    expect(groupMembersPath(7, 1)).toBe('/admin/groups/7/members?page=1&size=25')
    expect(groupMembersPath(7, 3)).toBe('/admin/groups/7/members?page=3&size=25')
    expect(MEMBERS_PAGE_SIZE).toBe(25)
  })
})

describe('groupMembersQueryKey', () => {
  it('erzeugt bei einem Seitenwechsel einen neuen Key -- Pagination triggert einen Re-Fetch', () => {
    const p1 = groupMembersQueryKey(7, 1)
    const p2 = groupMembersQueryKey(7, 2)
    expect(p1).toEqual(['group-members', 7, 1])
    expect(p2).toEqual(['group-members', 7, 2])
    expect(p1).not.toEqual(p2)
  })

  it('scoped auf die Gruppen-ID, damit parallel geöffnete Gruppen sich nicht überschreiben', () => {
    expect(groupMembersQueryKey(7, 1)).not.toEqual(groupMembersQueryKey(8, 1))
  })
})

describe('groupSyncPath', () => {
  it('baut den Sync-Endpoint je Gruppe', () => {
    expect(groupSyncPath(42)).toBe('/admin/groups/42/sync')
  })
})

describe('isLastMembersPage', () => {
  it('erste von zwei Seiten -> "Weiter" bleibt aktiv', () => {
    expect(isLastMembersPage(1, 30)).toBe(false)
  })

  it('letzte von zwei Seiten -> "Weiter" wird deaktiviert', () => {
    expect(isLastMembersPage(2, 30)).toBe(true)
  })

  it('total genau durch size teilbar -> die volle Seite ist bereits die letzte', () => {
    expect(isLastMembersPage(1, 25)).toBe(true)
  })

  it('leere Gruppe (total=0) -> sofort letzte Seite', () => {
    expect(isLastMembersPage(1, 0)).toBe(true)
  })
})

describe('memberDisplayName', () => {
  it('bevorzugt display_name', () => {
    expect(
      memberDisplayName({ display_name: 'Ada Lovelace', upn: 'ada@example.com' }),
    ).toBe('Ada Lovelace')
  })

  it('fällt auf die UPN zurück, wenn display_name null ist', () => {
    expect(memberDisplayName({ display_name: null, upn: 'ada@example.com' })).toBe(
      'ada@example.com',
    )
  })
})

describe('hasNeverSynced', () => {
  it('true nur bei null -- steuert den "noch nie synchronisiert"-Platzhalter', () => {
    expect(hasNeverSynced(null)).toBe(true)
  })

  it('false, sobald ein Zeitpunkt vorliegt', () => {
    expect(hasNeverSynced('2026-07-15T10:00:00Z')).toBe(false)
  })
})

describe('syncToastParams', () => {
  it('mappt member_count/materialized auf die Interpolationswerte des Erfolgs-Toasts', () => {
    expect(
      syncToastParams({ member_count: 12, materialized: 12, added: 2, removed: 0 }),
    ).toEqual({ count: 12, materialized: 12 })
  })

  it('added/removed fliessen bewusst nicht in den Toast ein', () => {
    const params = syncToastParams({ member_count: 5, materialized: 5, added: 5, removed: 0 })
    expect(params).not.toHaveProperty('added')
    expect(params).not.toHaveProperty('removed')
  })
})
