export type AuthSession = {
  access_token: string
  refresh_token: string
  expires_at: number
  user: { id: string; email: string | null }
}

const STORAGE_KEY = 'dugout.supabase.session'
const SUPABASE_URL = (import.meta.env.VITE_SUPABASE_URL ?? '').replace(/\/$/, '')
const SUPABASE_KEY = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY ?? import.meta.env.VITE_SUPABASE_ANON_KEY ?? ''

export function authConfigured(): boolean {
  return Boolean(SUPABASE_URL && SUPABASE_KEY)
}

export function loadAuthSession(): AuthSession | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) as AuthSession : null
  } catch {
    window.localStorage.removeItem(STORAGE_KEY)
    return null
  }
}

function saveSession(payload: Record<string, unknown>): AuthSession {
  const user = payload.user as { id?: string; email?: string | null } | undefined
  const session: AuthSession = {
    access_token: String(payload.access_token ?? ''),
    refresh_token: String(payload.refresh_token ?? ''),
    expires_at: Math.floor(Date.now() / 1000) + Number(payload.expires_in ?? 3600),
    user: { id: String(user?.id ?? ''), email: user?.email ?? null },
  }
  if (!session.access_token || !session.refresh_token || !session.user.id) throw new Error('로그인 응답 형식이 올바르지 않습니다.')
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
  return session
}

async function authRequest(path: string, body: Record<string, string>): Promise<Record<string, unknown>> {
  if (!authConfigured()) throw new Error('프런트엔드에 Supabase 로그인 환경변수가 설정되지 않았습니다.')
  const response = await fetch(`${SUPABASE_URL}${path}`, {
    method: 'POST',
    headers: { apikey: SUPABASE_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) throw new Error(String(payload.msg ?? payload.error_description ?? payload.message ?? '로그인 요청에 실패했습니다.'))
  return payload
}

export async function signInWithPassword(email: string, password: string): Promise<AuthSession> {
  return saveSession(await authRequest('/auth/v1/token?grant_type=password', { email, password }))
}

async function refreshSession(session: AuthSession): Promise<AuthSession> {
  return saveSession(await authRequest('/auth/v1/token?grant_type=refresh_token', { refresh_token: session.refresh_token }))
}

export async function getAccessToken(): Promise<string> {
  const session = loadAuthSession()
  if (!session) throw new Error('로그인이 필요합니다.')
  if (session.expires_at > Math.floor(Date.now() / 1000) + 60) return session.access_token
  try {
    return (await refreshSession(session)).access_token
  } catch (error) {
    window.localStorage.removeItem(STORAGE_KEY)
    throw error
  }
}

export async function signOut(): Promise<void> {
  const session = loadAuthSession()
  window.localStorage.removeItem(STORAGE_KEY)
  if (!session || !authConfigured()) return
  await fetch(`${SUPABASE_URL}/auth/v1/logout`, {
    method: 'POST',
    headers: { apikey: SUPABASE_KEY, Authorization: `Bearer ${session.access_token}` },
  }).catch(() => undefined)
}
