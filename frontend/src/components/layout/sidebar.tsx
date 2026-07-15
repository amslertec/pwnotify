import { Bell, History, LayoutDashboard, PanelLeftClose, ScrollText, Settings, UserCog, Users } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LanguageSwitcher } from './language-switcher'
import { Logo } from '../logo'
import { Button } from '../ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '../ui/tooltip'
import { useAuth } from '@/lib/auth'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/', labelKey: 'nav.dashboard', icon: LayoutDashboard, end: true, adminOnly: false },
  { to: '/users', labelKey: 'nav.users', icon: Users, end: false, adminOnly: false },
  { to: '/notifications', labelKey: 'nav.notifications', icon: Bell, end: false, adminOnly: false },
  { to: '/runs', labelKey: 'nav.runs', icon: History, end: false, adminOnly: false },
  { to: '/access', labelKey: 'nav.access', icon: UserCog, end: false, adminOnly: true },
  { to: '/audit', labelKey: 'nav.audit', icon: ScrollText, end: false, adminOnly: true },
  { to: '/settings', labelKey: 'nav.settings', icon: Settings, end: false, adminOnly: true },
]

export function Sidebar({
  collapsed,
  onToggle,
  onNavigate,
}: {
  collapsed: boolean
  onToggle?: () => void
  onNavigate?: () => void
}) {
  const { t } = useTranslation()
  const isAdmin = useAuth().user?.role === 'admin'
  const nav = NAV.filter((item) => isAdmin || !item.adminOnly)
  return (
    <aside
      className={cn(
        'border-sidebar-border bg-sidebar flex h-full flex-col border-r transition-[width] duration-200',
        collapsed ? 'w-16' : 'w-64',
      )}
    >
      <div
        className={cn(
          'border-sidebar-border flex h-16 items-center border-b px-4',
          collapsed ? 'justify-center' : 'justify-between',
        )}
      >
        <NavLink to="/" aria-label={t('nav.dashboard')} className="rounded-md">
          <Logo collapsed={collapsed} />
        </NavLink>
        {!collapsed && onToggle && (
          <Button variant="ghost" size="icon" onClick={onToggle} aria-label={t('nav.collapse')}>
            <PanelLeftClose className="size-4" />
          </Button>
        )}
      </div>

      <nav className="flex-1 space-y-1 overflow-y-auto p-3">
        {nav.map((item) => {
          const link = (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                  collapsed && 'justify-center px-0',
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-sidebar-foreground hover:bg-muted/70 hover:text-foreground',
                )
              }
            >
              <item.icon className="size-[1.15rem] shrink-0" />
              {!collapsed && <span>{t(item.labelKey)}</span>}
            </NavLink>
          )
          return collapsed ? (
            <Tooltip key={item.to}>
              <TooltipTrigger asChild>{link}</TooltipTrigger>
              <TooltipContent side="right">{t(item.labelKey)}</TooltipContent>
            </Tooltip>
          ) : (
            link
          )
        })}
      </nav>

      <LanguageSwitcher collapsed={collapsed} />

      {collapsed && onToggle && (
        <div className="border-sidebar-border border-t p-3">
          <Button
            variant="ghost"
            size="icon"
            onClick={onToggle}
            className="mx-auto"
            aria-label={t('nav.collapse')}
          >
            <PanelLeftClose className="size-4 rotate-180" />
          </Button>
        </div>
      )}
    </aside>
  )
}
