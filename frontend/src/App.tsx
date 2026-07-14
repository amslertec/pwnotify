import { useQuery } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { FullScreenLoader } from './components/full-screen-loader'
import { InstallPrompt } from './components/install-prompt'
import { AppLayout } from './components/layout/app-layout'
import { Toaster } from './components/toaster'
import { api } from './lib/api'
import { useAuth } from './lib/auth'
import type { SetupStatus } from './lib/types'
import AccessPage from './pages/access'
import DashboardPage from './pages/dashboard'
import LoginPage from './pages/login'
import NotificationsPage from './pages/notifications'
import ProfilePage from './pages/profile'
import RunsPage from './pages/runs'
import SettingsPage from './pages/settings'
import SetupPage from './pages/setup'
import UsersPage from './pages/users'

function useSetupStatus() {
  return useQuery({
    queryKey: ['setup-status'],
    queryFn: () => api.get<SetupStatus>('/setup/status'),
    staleTime: 30_000,
  })
}

function Guarded() {
  const { user, loading } = useAuth()
  const { data: setup, isLoading } = useSetupStatus()
  if (loading || isLoading) return <FullScreenLoader />
  if (setup?.needs_setup) return <Navigate to="/setup" replace />
  if (!user) return <Navigate to="/login" replace />
  return <AppLayout />
}

export default function App() {
  return (
    <BrowserRouter>
      <Toaster />
      <InstallPrompt />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup" element={<SetupPage />} />
        <Route element={<Guarded />}>
          <Route index element={<DashboardPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/access" element={<AccessPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/notifications" element={<NotificationsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
