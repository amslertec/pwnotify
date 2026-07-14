import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

import { api, ApiError, onAuthExpired } from './api'
import type { User } from './types'

interface AuthContextValue {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const qc = useQueryClient()

  const refresh = async () => {
    try {
      setUser(await api.get<User>('/auth/me'))
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    onAuthExpired.handler = () => {
      setUser(null)
      qc.clear()
    }
    return () => {
      onAuthExpired.handler = null
    }
  }, [qc])

  const login = async (username: string, password: string) => {
    const u = await api.post<User>('/auth/login', { username, password })
    setUser(u)
  }

  const logout = async () => {
    try {
      await api.post('/auth/logout')
    } catch (e) {
      if (!(e instanceof ApiError)) throw e
    }
    setUser(null)
    qc.clear()
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth muss innerhalb von AuthProvider verwendet werden')
  return ctx
}
