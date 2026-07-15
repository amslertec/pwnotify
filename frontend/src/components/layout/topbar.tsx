import { LogOut, Menu, User as UserIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate } from 'react-router-dom'

import { ThemeToggle } from '../theme-toggle'
import { Button } from '../ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu'
import { UserAvatar } from '../user-avatar'
import { useAuth } from '@/lib/auth'

const TITLE_KEYS: Record<string, string> = {
  '/': 'nav.dashboard',
  '/users': 'nav.users',
  '/access': 'nav.access',
  '/profile': 'nav.profile',
  '/notifications': 'nav.notifications',
  '/runs': 'nav.runs',
  '/audit': 'nav.audit',
  '/settings': 'nav.settings',
}

export function Topbar({ onMenu }: { onMenu: () => void }) {
  const { pathname } = useLocation()
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const crumbKey = TITLE_KEYS[pathname] ?? TITLE_KEYS['/' + pathname.split('/')[1]]
  const crumb = crumbKey ? t(crumbKey) : 'PwNotify'

  return (
    <header className="border-border bg-background/80 sticky top-0 z-30 flex h-16 items-center gap-3 border-b px-4 backdrop-blur-md md:px-6">
      <Button variant="ghost" size="icon" className="lg:hidden" onClick={onMenu} aria-label={t('nav.menu')}>
        <Menu className="size-5" />
      </Button>

      <div className="text-muted-foreground flex items-center gap-2 text-sm">
        <span className="text-foreground font-medium">{crumb}</span>
      </div>

      <div className="ml-auto flex items-center gap-1">
        <ThemeToggle />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" className="gap-2 pr-3 pl-2">
              <UserAvatar className="size-7 text-xs" />
              <span className="hidden max-w-[180px] truncate text-sm font-medium sm:block">
                {user?.display_name || user?.username}
              </span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel className="truncate">
              {user?.display_name || user?.username}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => navigate('/profile')}>
              <UserIcon /> {t('nav.profile')}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem destructive onClick={() => void logout()}>
              <LogOut /> {t('nav.logout')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  )
}
