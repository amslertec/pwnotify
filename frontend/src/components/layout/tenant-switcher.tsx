import { Building2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '../ui/tooltip'
import { useAuth } from '@/lib/auth'
import type { User } from '@/lib/types'

/** Reine Sichtbarkeits-Prüfung (aus der Komponente gezogen, damit sie ohne Rendering
 *  testbar ist). Sichtbar, wenn (a) die instanzweite Mandantenfähigkeit eingeschaltet
 *  ist (Task 7 — Default ist AUS) UND (b) das Konto zu mehr als einem Kunden wechseln
 *  darf (Single-Tenant-Konten sehen wie bisher nichts). Bewusst UNABHÄNGIG von
 *  `active_tenant_is_default` (Context-Gating v2, Task 5): der Umschalter muss auch in
 *  einem Kunden-Kontext sichtbar bleiben, damit der Superadmin zurück in den
 *  Default-Kontext wechseln kann. */
export function isSwitcherVisible(user: User | null | undefined): user is User {
  return !!user && user.multi_tenant_mode && user.switchable_tenants.length > 1
}

export function TenantSwitcher({ collapsed }: { collapsed: boolean }) {
  const { t } = useTranslation()
  const { user, switchTenant } = useAuth()

  if (!isSwitcherVisible(user)) return null

  const activeId = user.active_tenant ? String(user.active_tenant.id) : undefined
  const activeName = user.active_tenant?.name ?? t('tenant.switcher_label')

  const change = (value: string) => {
    const id = Number(value)
    if (user.active_tenant && id === user.active_tenant.id) return
    void switchTenant(id)
  }

  if (collapsed) {
    return (
      <div className="border-sidebar-border mb-2 border-b pb-2">
        <Select value={activeId} onValueChange={change}>
          <Tooltip>
            <TooltipTrigger asChild>
              <SelectTrigger
                className="text-muted-foreground hover:text-foreground hover:bg-muted/70 mx-auto h-9 w-9 justify-center gap-0 border-0 bg-transparent px-0 shadow-none [&>svg:last-child]:hidden"
                aria-label={t('tenant.switcher_label')}
              >
                <Building2 className="size-[1.15rem]" />
              </SelectTrigger>
            </TooltipTrigger>
            <TooltipContent side="right">{activeName}</TooltipContent>
          </Tooltip>
          <SelectContent>
            {user.switchable_tenants.map((tenant) => (
              <SelectItem key={tenant.id} value={String(tenant.id)}>
                {tenant.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    )
  }

  return (
    <div className="border-sidebar-border mb-2 space-y-2 border-b pb-3">
      <div className="text-muted-foreground flex items-center gap-2 px-1 text-xs font-medium">
        <Building2 className="size-3.5" /> {t('tenant.switcher_label')}
      </div>
      <Select value={activeId} onValueChange={change}>
        <SelectTrigger
          aria-label={t('tenant.switcher_label')}
          title={`${t('tenant.current')}: ${activeName}`}
        >
          <SelectValue placeholder={t('tenant.switcher_label')} />
        </SelectTrigger>
        <SelectContent>
          {user.switchable_tenants.map((tenant) => (
            <SelectItem
              key={tenant.id}
              value={String(tenant.id)}
              title={t('tenant.switch_to', { name: tenant.name })}
            >
              {tenant.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
