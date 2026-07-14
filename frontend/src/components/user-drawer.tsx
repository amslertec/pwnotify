import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Mail, Send } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { ExpiryRing } from './expiry-ring'
import { Button } from './ui/button'
import { Label } from './ui/label'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from './ui/sheet'
import { Switch } from './ui/switch'
import { api, ApiError } from '@/lib/api'
import { fmtDate, fmtDateTime } from '@/lib/format'
import type { EntraUserDetail, Notification, Page } from '@/lib/types'

export function UserDrawer({
  userId,
  open,
  onOpenChange,
}: {
  userId: number | null
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const [showRaw, setShowRaw] = useState(false)

  const { data: user } = useQuery({
    queryKey: ['user', userId],
    queryFn: () => api.get<EntraUserDetail>(`/users/${userId}`),
    enabled: userId !== null,
  })
  const { data: logs } = useQuery({
    queryKey: ['user-notifications', userId],
    queryFn: () => api.get<Page<Notification>>(`/notifications?user_id=${userId}&page_size=50`),
    enabled: userId !== null,
  })

  const exclude = useMutation({
    mutationFn: (excluded: boolean) => api.post(`/users/${userId}/exclude`, { excluded }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['user', userId] })
      void qc.invalidateQueries({ queryKey: ['users'] })
    },
  })
  const notify = useMutation({
    mutationFn: () => api.post<{ message: string }>(`/users/${userId}/notify`),
    onSuccess: (r) => {
      toast.success(r.message)
      void qc.invalidateQueries({ queryKey: ['user-notifications', userId] })
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.message : 'Fehler'),
  })

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-xl">
        {user && (
          <>
            <SheetHeader>
              <div className="flex items-center gap-4">
                <ExpiryRing user={user} size={52} />
                <div className="min-w-0">
                  <SheetTitle className="truncate">{user.display_name}</SheetTitle>
                  <p className="text-muted-foreground truncate font-mono text-xs">{user.upn}</p>
                </div>
              </div>
            </SheetHeader>

            <div className="flex-1 space-y-6 overflow-y-auto p-5">
              {/* Aktionen */}
              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={() => notify.mutate()} loading={notify.isPending} size="sm">
                  <Send /> Erinnerung jetzt senden
                </Button>
                <div className="flex items-center gap-2">
                  <Switch
                    id="exclude"
                    checked={user.excluded}
                    onCheckedChange={(v) => exclude.mutate(v)}
                  />
                  <Label htmlFor="exclude" className="cursor-pointer">
                    Von Benachrichtigungen ausschliessen
                  </Label>
                </div>
              </div>

              {/* Fakten */}
              <div className="grid grid-cols-2 gap-3">
                <Fact
                  label="Primär-Mail"
                  value={user.mail ?? '—'}
                  icon={<Mail className="size-3.5" />}
                />
                <Fact label="Alternativ-Mail" value={user.other_mails.join(', ') || '—'} />
                <Fact label="Abteilung" value={user.department ?? '—'} />
                <Fact label="Position" value={user.job_title ?? '—'} />
                <Fact
                  label="Passwort zuletzt geändert"
                  value={fmtDate(user.last_password_change)}
                />
                <Fact label="Ablaufdatum" value={fmtDate(user.expiry_date)} />
                <Fact
                  label="Verbleibend"
                  value={user.days_left == null ? 'Kein Ablauf' : `${user.days_left} Tage`}
                />
                <Fact label="Konto" value={user.account_enabled ? 'Aktiv' : 'Deaktiviert'} />
                {user.password_policies && (
                  <Fact label="Policies" value={user.password_policies} className="col-span-2" />
                )}
              </div>

              {/* Historie */}
              <div>
                <h4 className="font-display mb-2 flex items-center gap-2 text-sm font-semibold">
                  <Bell className="size-4" /> Benachrichtigungs-Historie
                </h4>
                {logs && logs.items.length > 0 ? (
                  <div className="space-y-1.5">
                    {logs.items.map((n) => (
                      <div
                        key={n.id}
                        className="border-border flex items-center gap-3 rounded-lg border px-3 py-2 text-sm"
                      >
                        <span
                          className="size-2 rounded-full"
                          style={{
                            background:
                              n.status === 'sent' ? 'var(--status-ok)' : 'var(--status-expired)',
                          }}
                        />
                        <span className="font-medium">{n.reminder_day} T</span>
                        <span className="text-muted-foreground truncate">{n.recipient}</span>
                        <span className="text-muted-foreground ml-auto text-xs whitespace-nowrap">
                          {fmtDateTime(n.created_at)}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-muted-foreground text-sm">Noch keine Benachrichtigungen.</p>
                )}
              </div>

              {/* Rohdaten */}
              <div>
                <button
                  className="text-primary text-sm font-medium hover:underline"
                  onClick={() => setShowRaw((s) => !s)}
                >
                  {showRaw ? 'Graph-Rohdaten ausblenden' : 'Graph-Rohdaten anzeigen'}
                </button>
                {showRaw && (
                  <pre className="border-border bg-muted/50 mt-2 max-h-72 overflow-auto rounded-lg border p-3 font-mono text-xs">
                    {JSON.stringify(user.raw, null, 2)}
                  </pre>
                )}
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function Fact({
  label,
  value,
  icon,
  className,
}: {
  label: string
  value: string
  icon?: React.ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      <p className="text-muted-foreground flex items-center gap-1 text-xs">
        {icon}
        {label}
      </p>
      <p className="mt-0.5 truncate text-sm font-medium">{value}</p>
    </div>
  )
}
