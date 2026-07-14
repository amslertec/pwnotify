import { useState } from 'react'
import { Outlet } from 'react-router-dom'

import { Sheet, SheetContent } from '../ui/sheet'
import { Sidebar } from './sidebar'
import { Topbar } from './topbar'

const COLLAPSE_KEY = 'pwnotify-sidebar-collapsed'

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const [mobileOpen, setMobileOpen] = useState(false)

  const toggle = () => {
    setCollapsed((c) => {
      localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1')
      return !c
    })
  }

  return (
    <div className="flex h-full">
      {/* Desktop-Sidebar */}
      <div className="hidden lg:block">
        <Sidebar collapsed={collapsed} onToggle={toggle} />
      </div>

      {/* Mobile-Sidebar als Sheet */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-64 p-0">
          <Sidebar collapsed={false} onNavigate={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar onMenu={() => setMobileOpen(true)} />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1400px] p-4 md:p-6 lg:p-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
