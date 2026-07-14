import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, RefreshCw, ShieldCheck, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
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
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { translateError } from '@/lib/errors'
import { fmtDate, fmtRelative } from '@/lib/format'
import type { AdminUser, AdminUsers } from '@/lib/types'
import { initials } from '@/lib/utils'

export default function AccessPage() {
  const { t } = useTranslation()
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
    onError: (e) => toast.error(translateError(e)),
  })

  const sync = useMutation({
    mutationFn: () => api.post<{ message: string }>('/admin/users/sso/sync'),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <div>
      <PageHeader title={t('access.title')} description={t('access.description')} />

      <Tabs defaultValue="local">
        <TabsList>
          <TabsTrigger value="local">
            <KeyRound className="size-4" /> {t('access.tabLocal')}
          </TabsTrigger>
          <TabsTrigger value="sso">
            <ShieldCheck className="size-4" /> {t('access.tabSso')}
          </TabsTrigger>
        </TabsList>

        {/* Lokale Benutzer */}
        <TabsContent value="local">
          <div className="mb-3 flex justify-end">
            <Button onClick={() => setCreateOpen(true)}>
              <UserPlus /> {t('access.newLocalUser')}
            </Button>
          </div>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                    <th className="px-4 py-3 font-medium">{t('access.colName')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colUsername')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colLastLogin')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colCreated')}</th>
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
              <RefreshCw className="size-3.5" /> {t('access.syncEntra')}
            </Button>
          </div>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-border text-muted-foreground border-b text-left text-xs uppercase">
                    <th className="px-4 py-3 font-medium">{t('access.colName')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colUpn')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colStatus')}</th>
                    <th className="px-4 py-3 font-medium">{t('access.colLastLogin')}</th>
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
                        {t('access.noSsoUsers')}
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
  const { t } = useTranslation()
  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className="bg-primary/10 text-primary grid size-8 shrink-0 place-items-center rounded-full text-xs font-semibold">
            {initials(u.display_name || u.username)}
          </span>
          <span className="font-medium">{u.display_name || '—'}</span>
          {isSelf && <Badge variant="secondary">{t('access.you')}</Badge>}
        </div>
      </td>
      <td className="text-muted-foreground max-w-[260px] truncate px-4 py-2.5 font-mono text-xs">
        {u.username}
      </td>
      {sso && (
        <td className="px-4 py-2.5">
          <span className="inline-flex items-center gap-1.5 text-xs">
            <StatusDot status={u.is_active ? 'ok' : 'disabled'} />
            {u.is_active ? t('access.statusActive') : t('access.statusDisabled')}
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
          aria-label={t('access.delete')}
          title={!canDelete ? t('access.cannotDelete') : t('access.delete')}
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
  const { t } = useTranslation()
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
      toast.success(t('access.userCreated'))
      void qc.invalidateQueries({ queryKey: ['admin-users'] })
      setFirstName('')
      setLastName('')
      setUsername('')
      setPassword('')
      onOpenChange(false)
    },
    onError: (e) => toast.error(translateError(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('access.createTitle')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('access.firstName')}</Label>
              <Input value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>{t('access.lastName')}</Label>
              <Input value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>{t('access.username')}</Label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('access.passwordLabel')}</Label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={username.length < 3 || password.length < 10}
          >
            {t('access.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
