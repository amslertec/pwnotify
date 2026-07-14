import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, RefreshCw, ShieldCheck, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { PageHeader } from '@/components/page-header'
import { StatusDot } from '@/components/status-badge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { fmtDate, fmtRelative } from '@/lib/format'
import type { AdminUser, AdminUsers } from '@/lib/types'
import { initials } from '@/lib/utils'

export default function AccessPage() {
  const qc = useQueryClient()
  const { user: me } = useAuth()
  const [createOpen, setCreateOpen] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.get<AdminUsers>('/admin/users'),
  })

  const total = (data?.local.length ?? 0) + (data?.sso.length ?? 0)

  const del = useMutation({
    mutationFn: (id: number) => api.del<{ message: string }>(`/admin/users/${id}`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  const sync = useMutation({
    mutationFn: () => api.post<{ message: string }>('/admin/users/sso/sync'),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  return (
    <div>
      <PageHeader
        title="Benutzerverwaltung"
        description="Lokale Konten und per SSO berechtigte Konten."
      />

      <Tabs defaultValue="local">
        <TabsList>
          <TabsTrigger value="local">
            <KeyRound className="size-4" /> Lokale Benutzer
          </TabsTrigger>
          <TabsTrigger value="sso">
            <ShieldCheck className="size-4" /> SSO-Benutzer
          </TabsTrigger>
        </TabsList>

        {/* Lokale Benutzer */}
        <TabsContent value="local">
          <div className="mb-3 flex justify-end">
            <Button onClick={() => setCreateOpen(true)}>
              <UserPlus /> Lokaler Benutzer
            </Button>
          </div>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                    <th className="px-4 py-3 font-medium">Name</th>
                    <th className="px-4 py-3 font-medium">Benutzername</th>
                    <th className="px-4 py-3 font-medium">Letzte Anmeldung</th>
                    <th className="px-4 py-3 font-medium">Erstellt</th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody className="divide-border divide-y">
                  {isLoading ? (
                    <tr>
                      <td colSpan={5} className="p-4">
                        <Skeleton className="h-8 w-full" />
                      </td>
                    </tr>
                  ) : (
                    data?.local.map((u) => (
                      <UserRow
                        key={u.id}
                        u={u}
                        isSelf={u.id === me?.id}
                        canDelete={total > 1 && u.id !== me?.id}
                        onDelete={() => del.mutate(u.id)}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </TabsContent>

        {/* SSO-Benutzer */}
        <TabsContent value="sso">
          <div className="mb-3 flex justify-end">
            <Button variant="outline" onClick={() => sync.mutate()} loading={sync.isPending}>
              <RefreshCw className="size-3.5" /> Mit Entra-Gruppe synchronisieren
            </Button>
          </div>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                    <th className="px-4 py-3 font-medium">Name</th>
                    <th className="px-4 py-3 font-medium">UPN</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Letzte Anmeldung</th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody className="divide-border divide-y">
                  {isLoading ? (
                    <tr>
                      <td colSpan={5} className="p-4">
                        <Skeleton className="h-8 w-full" />
                      </td>
                    </tr>
                  ) : data && data.sso.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="text-muted-foreground px-4 py-8 text-center">
                        Keine SSO-Benutzer. Synchronisieren, sobald SSO aktiv ist.
                      </td>
                    </tr>
                  ) : (
                    data?.sso.map((u) => (
                      <UserRow
                        key={u.id}
                        u={u}
                        sso
                        isSelf={u.id === me?.id}
                        canDelete={total > 1 && u.id !== me?.id}
                        onDelete={() => del.mutate(u.id)}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </TabsContent>
      </Tabs>

      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

function UserRow({
  u,
  sso,
  isSelf,
  canDelete,
  onDelete,
}: {
  u: AdminUser
  sso?: boolean
  isSelf: boolean
  canDelete: boolean
  onDelete: () => void
}) {
  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className="bg-primary/10 text-primary grid size-8 shrink-0 place-items-center rounded-full text-xs font-semibold">
            {initials(u.display_name || u.username)}
          </span>
          <span className="font-medium">{u.display_name || '—'}</span>
          {isSelf && <Badge variant="secondary">Sie</Badge>}
        </div>
      </td>
      <td className="text-muted-foreground max-w-[260px] truncate px-4 py-2.5 font-mono text-xs">
        {u.username}
      </td>
      {sso && (
        <td className="px-4 py-2.5">
          <span className="inline-flex items-center gap-1.5 text-xs">
            <StatusDot status={u.is_active ? 'ok' : 'disabled'} />
            {u.is_active ? 'Aktiv' : 'Deaktiviert'}
          </span>
        </td>
      )}
      <td className="text-muted-foreground px-4 py-2.5">{fmtRelative(u.last_login_at)}</td>
      {!sso && <td className="text-muted-foreground px-4 py-2.5">{fmtDate(u.created_at)}</td>}
      <td className="px-4 py-2.5 text-right">
        <Button
          variant="ghost"
          size="icon"
          disabled={!canDelete}
          onClick={onDelete}
          aria-label="Löschen"
          title={
            !canDelete ? 'Letzter Benutzer / eigenes Konto kann nicht gelöscht werden' : 'Löschen'
          }
        >
          <Trash2 className="text-danger size-4" />
        </Button>
      </td>
    </tr>
  )
}

function CreateDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const create = useMutation({
    mutationFn: () =>
      api.post('/admin/users', {
        username,
        password,
        display_name: `${firstName} ${lastName}`.trim() || null,
      }),
    onSuccess: () => {
      toast.success('Benutzer erstellt')
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      setFirstName('')
      setLastName('')
      setUsername('')
      setPassword('')
      onOpenChange(false)
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Lokalen Benutzer erstellen</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Vorname</Label>
              <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Nachname</Label>
              <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>Benutzername</Label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Passwort (mind. 10 Zeichen)</Label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={username.length < 3 || password.length < 10}
          >
            Erstellen
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
