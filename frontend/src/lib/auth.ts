import { createClient } from '@supabase/supabase-js'
import type { AuthChangeEvent, Session } from '@supabase/supabase-js'


export type AuthSession = Session

const SUPABASE_URL = (import.meta.env.VITE_SUPABASE_URL ?? '').replace(/\/$/, '')
const SUPABASE_KEY = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY ?? import.meta.env.VITE_SUPABASE_ANON_KEY ?? ''
const supabase = SUPABASE_URL && SUPABASE_KEY
  ? createClient(SUPABASE_URL, SUPABASE_KEY, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    })
  : null

export function authConfigured(): boolean {
  return Boolean(supabase)
}

function requireClient() {
  if (!supabase) throw new Error('프런트엔드에 Supabase 로그인 환경변수가 설정되지 않았습니다.')
  return supabase
}

export async function loadAuthSession(): Promise<AuthSession | null> {
  if (!supabase) return null
  const { data, error } = await supabase.auth.getSession()
  if (error) throw error
  return data.session
}

export function onAuthStateChange(callback: (session: AuthSession | null, event: AuthChangeEvent) => void) {
  if (!supabase) return () => undefined
  const { data } = supabase.auth.onAuthStateChange((event, session) => callback(session, event))
  return () => data.subscription.unsubscribe()
}

export async function signInWithPassword(email: string, password: string): Promise<AuthSession> {
  const { data, error } = await requireClient().auth.signInWithPassword({ email, password })
  if (error) throw error
  if (!data.session) throw new Error('로그인 세션을 만들지 못했습니다.')
  return data.session
}

export async function sendMagicLink(email: string): Promise<void> {
  const { error } = await requireClient().auth.signInWithOtp({
    email,
    options: {
      shouldCreateUser: false,
      emailRedirectTo: window.location.origin,
    },
  })
  if (error) throw error
}

export async function getAccessToken(): Promise<string> {
  const session = await loadAuthSession()
  if (!session) throw new Error('로그인이 필요합니다. 이메일 로그인 링크를 다시 받아 주세요.')
  return session.access_token
}

export async function signOut(): Promise<void> {
  if (!supabase) return
  const { error } = await supabase.auth.signOut()
  if (error) throw error
}
