import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '@/lib/api/endpoints'
import { clearToken, getToken, onUnauthorized, setToken } from '@/lib/api/client'

interface AuthUser {
  id: string
  email: string
}

interface AuthContextValue {
  user: AuthUser | null
  isLoading: boolean
  isAuthenticated: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = React.createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate()
  const [user, setUser] = React.useState<AuthUser | null>(null)
  // Only genuinely "loading" if there's a stored token to validate against
  // GET /me — known synchronously at mount, so it belongs in the initializer
  // rather than being flipped by an effect on the no-token path.
  const [isLoading, setIsLoading] = React.useState(() => !!getToken())

  React.useEffect(() => {
    onUnauthorized(() => {
      setUser(null)
      navigate('/login', { replace: true })
    })
  }, [navigate])

  // Bootstraps the session from a stored token by checking it against the
  // external system (GET /me) on mount — not derived state, so this effect
  // is the sanctioned "subscribe/sync with an external system" case.
  React.useEffect(() => {
    if (!getToken()) return
    let cancelled = false
    authApi
      .me()
      .then((me) => {
        if (!cancelled) setUser(me)
      })
      .catch(() => {
        if (!cancelled) setUser(null)
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const login = React.useCallback(async (email: string, password: string) => {
    const token = await authApi.login(email, password)
    setToken(token.access_token)
    const me = await authApi.me()
    setUser(me)
  }, [])

  const register = React.useCallback(
    async (email: string, password: string) => {
      await authApi.register({ email, password })
      await login(email, password)
    },
    [login],
  )

  const logout = React.useCallback(() => {
    clearToken()
    setUser(null)
    navigate('/login')
  }, [navigate])

  const value = React.useMemo(
    () => ({ user, isLoading, isAuthenticated: !!user, login, register, logout }),
    [user, isLoading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
