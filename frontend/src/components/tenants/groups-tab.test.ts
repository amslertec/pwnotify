import { describe, expect, it } from 'vitest'

import {
  createGroupBody,
  entraAvatarPath,
  groupMembersPath,
  groupMembersQueryKey,
  groupRoleBadgeVariant,
  groupSyncPath,
  hasNeverSynced,
  isLastMembersPage,
  memberDisplayName,
  MEMBERS_PAGE_SIZE,
  syncToastParams,
  updateGroupBody,
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

describe('entraAvatarPath', () => {
  it('baut den Foto-Endpoint aus der Entra-Objekt-GUID', () => {
    expect(entraAvatarPath('11111111-2222-3333-4444-555555555555')).toBe(
      '/api/entra-avatar/11111111-2222-3333-4444-555555555555',
    )
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

describe('groupRoleBadgeVariant', () => {
  it('admin bekommt die hervorgehobene Default-Variante', () => {
    expect(groupRoleBadgeVariant('admin')).toBe('default')
  })

  it('auditor bekommt die dezentere secondary-Variante', () => {
    expect(groupRoleBadgeVariant('auditor')).toBe('secondary')
  })
})

describe('createGroupBody', () => {
  it('nimmt role in den POST /admin/groups-Body auf (vom Backend erzwungen, sonst 422)', () => {
    expect(createGroupBody('Team A', 'grp-123', 'admin')).toEqual({
      name: 'Team A',
      entra_group_id: 'grp-123',
      role: 'admin',
    })
  })

  it('funktioniert auch mit role=auditor', () => {
    expect(createGroupBody('Team B', 'grp-456', 'auditor')).toEqual({
      name: 'Team B',
      entra_group_id: 'grp-456',
      role: 'auditor',
    })
  })
})

describe('updateGroupBody', () => {
  it('nimmt role in den PUT /admin/groups/{id}-Body auf, ohne entra_group_id (unveränderlich)', () => {
    const body = updateGroupBody('Neuer Name', 'auditor')
    expect(body).toEqual({ name: 'Neuer Name', role: 'auditor' })
    expect(body).not.toHaveProperty('entra_group_id')
  })
})
